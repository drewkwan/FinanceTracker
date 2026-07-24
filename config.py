"""
Configuration for the expense-tracking Telegram bot.
All values are read from environment variables so secrets never live in code.
See .env.example for the full list of variables you need to set.
"""

import os

# --- Required ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- Optional ---
# Only these Telegram user IDs may use the bot. Leave blank to allow anyone who
# has your bot's link (fine for a private bot, but locking it down is safer).
ALLOWED_CHAT_IDS = [
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
]

# Where the SQLite database file lives. On Railway, set this to a path inside
# a mounted volume so data survives redeploys (see README).
DB_PATH = os.environ.get("DB_PATH", "expenses.db")

# Single timezone used for "what day is it" (daily rollover, /summary today).
# This is a personal single-user-ish bot, so one global timezone keeps things
# simple. Use an IANA name, e.g. "America/Los_Angeles", "Asia/Singapore".
BOT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Singapore")

# Claude model used for categorization / natural-language expense parsing.
# Haiku is fast and cheap, which is plenty for this task.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Default daily target used the very first time a user interacts with the bot,
# before they run /settarget.
DEFAULT_DAILY_TARGET = float(os.environ.get("DEFAULT_DAILY_TARGET", "100"))

# All targets, balances, and totals are tracked in this currency. Expenses
# logged in a different currency are converted to this one (via fx.py) for
# budget math, while the original amount + currency is still kept on the
# expense record so you can see what you actually paid.
BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "SGD")

# Currencies fx.py knows how to convert (matches what the free Frankfurter/ECB
# rates API supports). Add more here if you need a currency that's missing —
# just check it's on https://www.frankfurter.app/currencies first.
KNOWN_CURRENCIES = [
    "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "GBP", "HKD", "HUF",
    "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD",
    "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "USD", "ZAR", "EUR",
]

# Fraction of today's available budget (target + rolled-over balance) that,
# once crossed, triggers a one-time heads-up message for the day.
BUDGET_ALERT_THRESHOLD = float(os.environ.get("BUDGET_ALERT_THRESHOLD", "0.9"))

CATEGORIES = [
    "Food",
    "Groceries",
    "Transport",
    "Entertainment",
    "Shopping",
    "Bills & Utilities",
    "Health",
    "Travel",
    "Other",
]
