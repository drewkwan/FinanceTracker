"""
Entrypoint: /start, /help, the background rollover tick, and main() itself
-- building the Application and registering every handler from every domain
module. This is the only place that needs to know about all of them at
once; every other module only imports the handful it actually depends on.
"""

import logging

from dotenv import load_dotenv
load_dotenv()  # must run before `import config`, which reads env vars at import time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import db
from access import _reject_if_not_allowed
from finance import (
    adjustbalance_cmd,
    balance,
    claim_expense,
    claimed,
    delete_cmd,
    edit_cmd,
    log_expense,
    recent,
    settarget,
    undo,
)
from fitness import logworkout_cmd, recentworkouts
from formatting import _money, _status_text
from handlers import handle_text, on_error
from memory import forget_cmd, memory_cmd
from nutrition import handle_photo, logmeal_cmd, recentmeals
from rundown import rundown_cmd
from summary import summary
from tasks import addtask_cmd, done_cmd, tasks_cmd
from vitals import logvitals_cmd, recentvitals

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _reject_if_not_allowed(update):
        return
    db.get_or_create_user(update.effective_chat.id)
    await update.message.reply_text(
        "Hey! I'll track your daily spending allowance, claimable expenses, meals, workouts, and vitals.\n\n"
        "Set a daily target: /settarget 100\n"
        "Log a personal expense: /log 12.50 lunch\n"
        "Log in another currency: /log 20 USD taxi\n"
        "Log a claimable one: /claim 300 groceries\n"
        "Cleared a reimbursement: /claimed\n"
        "Check where you stand: /balance\n"
        "Get a breakdown: /summary week\n"
        "See recent entries: /recent\n"
        "Fix a mistake: /undo, /edit <id> <amount>, or /delete <id>\n"
        "Manually correct rolled-over balance: /adjustbalance -1135.89\n\n"
        "Log a meal: /logmeal chicken rice and iced tea, or just send a photo of your food\n"
        "See recent meals: /recentmeals\n"
        "Log a workout: /logworkout tennis for an hour\n"
        "See recent workouts: /recentworkouts\n"
        "Log vitals: /logvitals weight 76.6, slept 5.5 hours, knee 2/10\n"
        "See recent check-ins: /recentvitals\n\n"
        "See what I remember: /memory\n"
        "Remove something remembered: /forget <label>\n\n"
        "Add a to-do: /addtask call the dentist tomorrow 5pm\n"
        "See what's open: /tasks\n"
        "Mark one done: /done <id>\n\n"
        "How's everything going, across money/food/training/vitals together: /rundown\n\n"
        "Or just tell me naturally, e.g. \"spent 15 on uber\", \"had a mango\", \"played tennis for an hour\", "
        "\"weight 76.6, slept 5.5 hours\", \"remember I go to Fitness First Bugis Tue/Thu\", \"remind me to call "
        "the dentist tomorrow\", or \"how am I doing this week\" -- and just talk to me the rest of the time, "
        "I'll keep up with the thread."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def rollover_tick(context: ContextTypes.DEFAULT_TYPE):
    """Runs periodically so rollovers happen even if no one messages the bot
    right at midnight. Notifies each user only if a rollover actually occurred."""
    for chat_id in db.get_all_chat_ids():
        result = db.ensure_rollover(chat_id)
        if not result["rollovers"]:
            continue
        status = db.get_status(chat_id)
        lines = [f"{d}: leftover {_money(leftover)}" for d, leftover in result["rollovers"]]
        text = "New day, balance rolled over:\n" + "\n".join(lines) + f"\n\n{_status_text(status)}"
        if result["new_best_streak"]:
            text += f"\n\nNew personal best streak: {result['new_best_streak']} days within budget!"
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.exception("Failed to notify chat %s of rollover", chat_id)


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. See .env.example.")
    if not config.ANTHROPIC_API_KEY:
        raise SystemExit("ANTHROPIC_API_KEY is not set. See .env.example.")

    db.init_db()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settarget", settarget))
    app.add_handler(CommandHandler("log", log_expense))
    app.add_handler(CommandHandler("claim", claim_expense))
    app.add_handler(CommandHandler("claimed", claimed))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("rundown", rundown_cmd))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("adjustbalance", adjustbalance_cmd))
    app.add_handler(CommandHandler("logmeal", logmeal_cmd))
    app.add_handler(CommandHandler("recentmeals", recentmeals))
    app.add_handler(CommandHandler("logworkout", logworkout_cmd))
    app.add_handler(CommandHandler("recentworkouts", recentworkouts))
    app.add_handler(CommandHandler("logvitals", logvitals_cmd))
    app.add_handler(CommandHandler("recentvitals", recentvitals))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("addtask", addtask_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    if app.job_queue is not None:
        app.job_queue.run_repeating(rollover_tick, interval=3600, first=10)
    else:
        logger.warning(
            "JobQueue not available -- install with pip install python-telegram-bot[job-queue] "
            "to get automatic daily rollover notifications. Rollover math still runs correctly "
            "whenever a user interacts with the bot."
        )

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
