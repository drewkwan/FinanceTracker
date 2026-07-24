# Personal Expense Tracker (Telegram bot)

Tracks a daily spending allowance with rollover, keeps claimable/reimbursable
expenses separate, supports multiple currencies, and uses Claude to categorize
purchases and answer free-form questions about your spending.

## How the money math works

- You set a **daily target** (e.g. `/settarget 100`).
- Each day, whatever you *don't* spend rolls into a **balance** that carries
  forward. Spend $30 of a $100 target and tomorrow's available amount is
  `$100 (new target) + $70 (yesterday's leftover) = $170`.
- Overspend and the balance goes negative, reducing what's available
  tomorrow — same mechanism, opposite direction.
- **Claimable expenses** (`/claim`) are tracked in a completely separate
  pool. They never touch your daily target or balance. Once reimbursed,
  `/claimed` clears all pending claimables.
- **Multi-currency**: your home currency is `BASE_CURRENCY` (defaults to
  SGD). Log an expense in another currency with `/log 20 USD taxi` and it's
  converted to your base currency (via the free Frankfurter/ECB rate API) for
  all balance math, while the original amount and currency are kept for
  display. If the currency lookup ever fails, it falls back to a 1:1
  conversion rather than blocking the log.
- **Streak**: consecutive days you stayed within your plain daily target
  (ignoring rolled-over balance) are tracked and shown in `/balance`. Hitting
  a new personal best triggers a one-time congrats message.
- **Budget alert**: once you cross `BUDGET_ALERT_THRESHOLD` (default 90%) of
  today's available budget, you get a one-time heads-up nudge for the day.
- **Trends** (on `/summary`, never pushed): the requested period is compared
  to the equal-length period right before it (this week vs last week, etc.),
  plus your average spend by day-of-week (over an 8-week lookback) and how
  many days in the period you stayed under target. Claude turns all of that
  into a few plain-language lines rather than a data dump — see "Usage" below.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Telegram handlers, command definitions, the periodic rollover job |
| `db.py` | SQLite schema + all balance/rollover/claimable/streak/alert logic |
| `fx.py` | Currency conversion (Frankfurter/ECB rates, cached, with 1:1 fallback) |
| `ai.py` | Claude calls: natural-language parsing, categorization, on-demand summaries |
| `config.py` | Reads all settings from environment variables |
| `requirements.txt` | Python dependencies |
| `Procfile` | Tells Railway how to run the bot as a background worker |
| `.env.example` | Template for the environment variables you need to set |

## 1. Create your Telegram bot

1. Open Telegram, search for **@BotFather**, and start a chat.
2. Send `/newbot`, give it a name and a username (must end in `bot`, e.g.
   `andrew_expenses_bot`).
3. BotFather replies with a token like `123456789:AAExampleTokenHere`. Copy it.
   (If you've used a token for another bot before, this is a brand-new one —
   tokens map 1:1 to a single bot account, so each bot needs its own.)
4. Optional but recommended: message **@userinfobot** to get your own
   Telegram numeric user ID, so you can lock the bot to just you via
   `ALLOWED_CHAT_IDS`.

## 2. Get an Anthropic API key

Go to <https://console.anthropic.com/settings/keys>, create a key. This is
billed separately from a Claude.ai subscription — usage here is tiny
(a few hundred short Haiku calls a month), typically well under $1.

## 3. Run it locally first (recommended sanity check)

```bash
cd telegram-expense-bot
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # then fill in TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
python bot.py
```

`.env` is loaded automatically (via `python-dotenv`) — no manual exporting
needed.

Message your bot on Telegram — `/start` should reply immediately.

## 4. Deploy to Railway (so it runs 24/7)

1. Push this folder to a GitHub repo (private is fine).
2. Go to <https://railway.app>, sign in, **New Project → Deploy from GitHub repo**,
   pick this repo.
3. Railway auto-detects Python. In the service's **Variables** tab, add
   everything from `.env.example` (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
   optionally `ALLOWED_CHAT_IDS`, `BOT_TIMEZONE`, etc.).
4. Under **Settings → Deploy**, make sure the start command matches the
   `Procfile` (`python bot.py`) — Railway usually picks this up automatically.
5. **Persistent storage matters**: by default Railway's filesystem resets on
   redeploy, which would wipe your SQLite database. Add a **Volume** (Railway
   dashboard → your service → Volumes) mounted at e.g. `/data`, then set the
   variable `DB_PATH=/data/expenses.db`.
6. Deploy. Check the logs for `Bot starting (polling)...`. Message your bot —
   it should respond from anywhere now, any time.

## Usage

```
/settarget 100                 set (or change) today's daily target
/log 12.50 lunch                log a personal expense
/log 20 USD taxi                same, but in another currency (auto-converted)
/claim 300 groceries             log a claimable/reimbursable expense
/claimed                         clear all pending claimables (mark reimbursed)
/balance                         target, balance, spent today, pending claimables, streak
/summary [today|week|month]      Claude-written breakdown + trends (see below)
/recent [n]                      last n logged expenses with their IDs (default 10)
/undo                            remove the single most recent expense
/edit <id> <amount> [desc...]    fix a mislogged expense
/delete <id>                     remove a specific expense by ID
```

Or skip commands and just type naturally:

> spent 15 on uber
> paid 300 for the team dinner, claimable
> got groceries for 84 bucks
> spent 20 USD on a hotel

The bot asks a quick follow-up only when it's genuinely unsure (missing
amount, ambiguous category, or a large charge with no claimable hint). It
only asks about currency if you never mention one — otherwise it defaults to
your base currency.

Undo/edit/delete are balance-safe: if you correct an expense logged on a
previous day (already rolled into your balance), the balance is adjusted
retroactively so the numbers stay accurate. Same-day corrections don't need
any adjustment since today's spend is always computed live.

`/summary` is entirely on-demand — nothing about trends or patterns is ever
pushed to you unprompted. Ask for it and it'll surface whatever's actually
worth knowing: how this period compares to the last one, which category moved
the most, whether a particular day of the week tends to be pricier, and how
many days you stayed under target. Thin or trivial signals (e.g. a change
under ~10%) are left out rather than padding the message.

## Notes / limitations

- This is built for a single primary user (you), though the schema supports
  multiple chats if you ever want to share it.
- `BOT_TIMEZONE` and `BASE_CURRENCY` are global settings (not per-user) —
  simplest correct choice for a personal bot.
- If the bot goes offline for a few days, the next time it's queried it
  rolls forward day-by-day using whatever your *current* daily target is —
  a reasonable approximation rather than perfect historical accuracy.
- Currency conversion calls a free, no-API-key exchange rate API
  (Frankfurter, ECB reference rates) and caches each rate for the day. If
  that lookup fails for any reason, the bot logs the expense at a 1:1 rate
  rather than blocking you — it never fails silently on the money math, but
  worth knowing rates could occasionally be off if the API is down.
- The streak counts days you stayed within your *plain daily target*, not
  your rolled-over balance — so it reflects daily discipline even if you're
  coasting on a big surplus.
