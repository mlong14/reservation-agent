"""
Telegram bot — the phone-facing superapp for the reservation agent.

Commands:
  /start   - Welcome + help
  /find    - Search for available slots and send proposals
  /status  - Show pending proposals + upcoming reservations
  /history - Show past bookings with feedback

Inline keyboard callbacks handled:
  book_{proposal_id}           - Confirm and execute a booking
  skip_{proposal_id}           - Dismiss a proposal
  feedback_{booking_id}_good   - Record positive feedback
  feedback_{booking_id}_bad    - Record negative feedback

Scheduled jobs:
  Mon/Wed/Fri 9:10 AM — auto-propose if below reservation target
  1st of month 2:00 AM — pipeline (Resy sync + classify + rebuild)
  Daily 9:00 AM        — 3-day reminders for upcoming reservations
  Daily 9:05 AM        — feedback requests for past dinners
"""

import asyncio
import datetime
import json
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import agent as agent_module
from agent import fmt_time
import google_services
import restaurant_pipeline
from booking_platforms import resy_client
from state import AgentState

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# --- Bot lifecycle ---

async def post_init(application: Application):
    """Initialize shared Google services once at startup."""
    gcal, sheets, gmail, drive = await asyncio.to_thread(google_services.get_google_services)
    application.bot_data["gcal"] = gcal
    application.bot_data["sheets"] = sheets
    application.bot_data["gmail"] = gmail
    application.bot_data["drive"] = drive
    logging.info("Google services initialized.")


# --- Command handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍽 *Reservation Agent*\n\n"
        "/find — Search for available slots\n"
        "/status — Pending proposals + upcoming reservations\n"
        "/history — Past bookings\n"
        "/pipeline — Expand restaurant list from Maps + web",
        parse_mode="Markdown",
    )


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = AgentState()
    if state.get_active_proposals():
        await update.message.reply_text(
            "You already have pending proposals below. Tap *Book it!* or *Skip* on them first.",
            parse_mode="Markdown",
        )
        return

    config = load_config()
    target = config.get("user", {}).get("reservation_target", 2)
    proposal_count = config.get("user", {}).get("proposal_count", 3)
    try:
        active = await asyncio.to_thread(resy_client.get_active_reservations, config["resy"])
        if len(active) >= target:
            await update.message.reply_text(
                f"You already have {len(active)} upcoming reservation(s) (target: {target}). All good!"
            )
            return
    except Exception as e:
        logging.error(f"Error checking reservations: {e}")

    msg = await update.message.reply_text("🔍 Searching and checking calendar context...")

    gcal = context.application.bot_data.get("gcal")
    sheets = context.application.bot_data.get("sheets")

    proposals = await asyncio.to_thread(agent_module.find_and_propose, config, gcal, sheets, proposal_count)

    if not proposals:
        await msg.edit_text("😕 No available slots found matching your preferences.")
        return

    await msg.delete()
    for proposal in proposals:
        await _send_proposal(context, update.effective_chat.id, proposal)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = AgentState()
    config = load_config()
    lines = []

    proposals = state.get_active_proposals()
    if proposals:
        lines.append(f"*Pending proposals ({len(proposals)}):*")
        for p in proposals:
            lines.append(f"  • {p['restaurant_name']} — {p['date']} at {p['time']}")
    else:
        lines.append("No pending proposals.")

    try:
        active = await asyncio.to_thread(resy_client.get_active_reservations, config["resy"])
        if active:
            lines.append(f"\n*Upcoming reservations ({len(active)}):*")
            for r in active:
                share_msg = r.get("share", {}).get("generic_message", "")
                if "Please RSVP for " in share_msg and " on " in share_msg:
                    name = share_msg.replace("Please RSVP for ", "").split(" on ")[0]
                else:
                    name = f"Venue {r.get('venue', {}).get('id', '?')}"
                day = r.get("day", "")
                time_slot = r.get("time_slot", "")
                if time_slot:
                    try:
                        import datetime as _dt
                        t = _dt.datetime.strptime(time_slot, "%H:%M:%S")
                        time_slot = t.strftime("%-I:%M %p")
                    except ValueError:
                        pass
                lines.append(f"  • {name} — {day} at {time_slot}")
        else:
            lines.append("\nNo upcoming reservations on Resy.")
    except Exception as e:
        lines.append(f"\n_(Could not fetch Resy reservations: {e})_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pipeline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    gcal = context.application.bot_data.get("gcal")
    sheets = context.application.bot_data.get("sheets")
    gmail = context.application.bot_data.get("gmail")
    drive = context.application.bot_data.get("drive")

    if not drive:
        await update.message.reply_text("❌ Drive service not available. Re-authenticate and restart the bot.")
        return

    msg = await update.message.reply_text("🔍 Running restaurant discovery pipeline...")
    try:
        counts = await asyncio.to_thread(
            restaurant_pipeline.run_pipeline, config, gcal, sheets, drive, gmail
        )
        await msg.edit_text(
            f"✅ *Pipeline Complete*\n\n"
            f"➕ Added to list: {counts['added']}\n"
            f"🚫 Not SF: {counts['not_sf']}\n"
            f"❌ Not on Resy: {counts['no_resy']}\n"
            f"⏭ Already processed: {counts['duplicate']}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.error(f"Pipeline failed: {e}")
        await msg.edit_text(f"❌ Pipeline failed: {e}")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = AgentState()
    bookings = sorted(state.get_all_bookings(), key=lambda b: b.get("booked_at", ""), reverse=True)

    if not bookings:
        await update.message.reply_text("No booking history yet.")
        return

    lines = ["*Booking History*\n"]
    for b in bookings[:15]:
        fb = {"good": " 👍", "bad": " 👎"}.get(b.get("feedback"), "")
        lines.append(f"• {b['restaurant_name']} ({b['date']}){fb}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --- Callback handler ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("book_"):
        await _handle_book(query, context, data[5:])
    elif data.startswith("skip_"):
        AgentState().remove_proposal(data[5:])
        await query.edit_message_text("⏭ Skipped. Run /find to search again.")
    elif data.startswith("feedback_"):
        _, booking_id, sentiment = data.split("_", 2)
        state = AgentState()
        state.save_feedback(booking_id, sentiment)
        booking = state.get_booking(booking_id)
        name = booking["restaurant_name"] if booking else "that restaurant"
        emoji = "👍" if sentiment == "good" else "👎"
        await query.edit_message_text(f"{emoji} Thanks for the feedback on *{name}*!", parse_mode="Markdown")


async def _handle_book(query, context: ContextTypes.DEFAULT_TYPE, proposal_id: str):
    state = AgentState()
    proposal = state.get_proposal(proposal_id)

    if not proposal:
        await query.edit_message_text("❌ This proposal has expired. Run /find to search again.")
        return

    await query.edit_message_text(f"⏳ Booking *{proposal['restaurant_name']}*...", parse_mode="Markdown")

    config = load_config()
    gcal = context.application.bot_data.get("gcal")

    result = await asyncio.to_thread(agent_module.do_booking, proposal, config, gcal)

    if result["success"]:
        state.remove_proposal(proposal_id)

        # Drop any remaining proposals that fall on the same date as the booking
        booked_date = proposal["date"]
        conflicts = [p for p in state.get_active_proposals() if p["date"] == booked_date]
        for p in conflicts:
            state.remove_proposal(p["id"])
            try:
                await context.bot.edit_message_text(
                    chat_id=query.message.chat_id,
                    message_id=p["telegram_message_id"],
                    text=f"🚫 _{p['restaurant_name']} on {booked_date} — cancelled, you already have a booking that day._",
                    parse_mode="Markdown",
                )
            except Exception:
                pass  # message may already be gone

        res_dt: datetime.datetime = result["reservation_datetime"]
        day_str = res_dt.strftime("%A, %B %-d at %-I:%M %p")
        cal_link = result.get("calendar_link", "")
        link_text = f"\n📆 [Added to calendar]({cal_link})" if cal_link else "\n📆 Added to calendar."

        await query.edit_message_text(
            f"✅ *Reservation Confirmed!*\n\n"
            f"📍 *{proposal['restaurant_name']}*\n"
            f"📅 {day_str}\n"
            f"👥 {proposal['party_size']} people\n"
            f"🎟 Confirmation: `{result['confirmation_id']}`"
            f"{link_text}",
            parse_mode="Markdown",
        )
    else:
        error = result.get("error", "Unknown error.")
        await query.edit_message_text(
            f"❌ Booking failed: {error}\n\nRun /find to search again."
        )


# --- Scheduled job ---

async def check_upcoming_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: send a reminder 3 days before an upcoming reservation."""
    config = load_config()
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        return

    state = AgentState()
    for booking in state.get_bookings_needing_reminder(days_ahead=3):
        res_dt = booking.get("reservation_datetime")
        time_str = datetime.datetime.fromisoformat(res_dt).strftime("%-I:%M %p") if res_dt else ""
        date_str = datetime.datetime.strptime(booking["date"], "%Y-%m-%d").strftime("%A, %B %-d")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⏰ *3-day reminder:* Dinner at *{booking['restaurant_name']}*\n"
                f"📅 {date_str} at {time_str}\n\n"
                f"Anything to change?"
            ),
            parse_mode="Markdown",
        )
        state.mark_reminder_sent(booking["id"])


async def check_pending_feedback(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: send feedback requests for dinners that have passed."""
    config = load_config()
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        return

    state = AgentState()
    for booking in state.get_bookings_needing_feedback():
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("👍 Great!", callback_data=f"feedback_{booking['id']}_good"),
            InlineKeyboardButton("👎 Not great", callback_data=f"feedback_{booking['id']}_bad"),
        ]])
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🍽 How was dinner at *{booking['restaurant_name']}*?",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        state.mark_feedback_requested(booking["id"], msg.message_id)


async def auto_propose(context: ContextTypes.DEFAULT_TYPE):
    """Mon/Wed/Fri job: send proposals if below reservation target."""
    config = load_config()
    chat_id = config.get("telegram", {}).get("chat_id")
    if not chat_id:
        return

    state = AgentState()
    if state.get_active_proposals():
        return  # already have pending proposals waiting on user

    target = config.get("user", {}).get("reservation_target", 2)
    proposal_count = config.get("user", {}).get("proposal_count", 3)
    try:
        active = await asyncio.to_thread(resy_client.get_active_reservations, config["resy"])
        if len(active) >= target:
            return
    except Exception:
        pass

    gcal = context.application.bot_data.get("gcal")
    sheets = context.application.bot_data.get("sheets")
    proposals = await asyncio.to_thread(agent_module.find_and_propose, config, gcal, sheets, proposal_count)

    for proposal in proposals:
        await _send_proposal(context, int(chat_id), proposal)


async def auto_pipeline(context: ContextTypes.DEFAULT_TYPE):
    """Monthly job: refresh Resy catalog, classify new venues, rebuild Restaurants tab."""
    config = load_config()
    chat_id = config.get("telegram", {}).get("chat_id")
    sheets = context.application.bot_data.get("sheets")
    gcal = context.application.bot_data.get("gcal")

    try:
        counts = await asyncio.to_thread(
            restaurant_pipeline.run_pipeline, config, gcal, sheets
        )
        if chat_id:
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"🔍 *Monthly pipeline complete*\n\n"
                    f"🏠 Resy: {counts.get('total_resy', 0)} total, {counts.get('new_resy', 0)} new\n"
                    f"🍽 Date spots: {counts.get('yes', 0)} yes / {counts.get('no', 0)} no / {counts.get('unknown', 0)} unknown\n"
                    f"✅ In restaurant list: {counts.get('passing', 0)}/{counts.get('total', 0)}"
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        logging.error(f"auto_pipeline error: {e}")
        if chat_id:
            await context.bot.send_message(chat_id=int(chat_id), text=f"⚠️ Pipeline error: {e}")


# --- Sending proposals from within the bot ---

async def _send_proposal(context: ContextTypes.DEFAULT_TYPE, chat_id: int, proposal: dict):
    date_display = datetime.datetime.strptime(proposal["date"], "%Y-%m-%d").strftime("%A, %B %-d")
    note = proposal.get("note", "")
    note_line = f"\n\n⚠️ _{note}_" if note else ""
    screen_line = "\n🤖 _Calendar screened_" if proposal.get("llm_screened") else "\n📋 _Basic calendar check only_"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Book it!", callback_data=f"book_{proposal['id']}"),
        InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{proposal['id']}"),
    ]])
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🍽 *Reservation Proposal*\n\n"
            f"📍 *{proposal['restaurant_name']}*\n"
            f"📅 {date_display} at {fmt_time(proposal['time'])}\n"
            f"👥 {proposal['party_size']} people\n"
            f"🎫 {proposal['platform'].title()}"
            f"{note_line}"
            f"{screen_line}"
        ),
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    AgentState().set_proposal_message_id(proposal["id"], msg.message_id)


# --- Main ---

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    config = load_config()
    bot_token = config.get("telegram", {}).get("bot_token")
    if not bot_token:
        raise ValueError("telegram.bot_token not set in config.json")

    app = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("pipeline", cmd_pipeline))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Daily jobs
    app.job_queue.run_daily(check_upcoming_reminders, time=datetime.time(9, 0, 0))
    app.job_queue.run_daily(check_pending_feedback, time=datetime.time(9, 5, 0))

    # Mon/Wed/Fri at 9:10 AM — send proposals if below target
    app.job_queue.run_daily(auto_propose, time=datetime.time(9, 10, 0), days=(0, 2, 4))

    # 1st of each month at 2:00 AM — refresh Resy catalog + classify + rebuild
    app.job_queue.run_monthly(auto_pipeline, when=datetime.time(2, 0, 0), day=1)

    logging.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
