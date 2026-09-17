# Personal Expense Tracker (Telegram bot)

Tracks a daily spending allowance with rollover, keeps claimable/reimbursable
expenses separate, supports multiple currencies, and uses Claude to categorize
purchases and answer free-form questions about your spending. Also tracks
meals (text or a photo, with a calorie range and a running daily total),
workouts, daily vitals check-ins (weight, sleep, knee pain), and to-dos with
optional due dates, all in the same database, through the same
natural-language pattern.

## How the money math works

- You set a **daily target** (e.g. `/settarget 100`).
- Each day, whatever you *don't* spend rolls into a **balance** that carries
  forward. Spend $30 of a $100 target and tomorrow's available amount is
  `$100 (new target) + $70 (yesterday's leftover) = $170`.
- Overspend and the balance goes negative, reducing what's available
  tomorrow — same mechanism, opposite direction.
- **Balance is always derived from logged expenses, never set directly** —
  it's rebuilt day by day from what's actually in the database, so it can
  never silently drift from your real history. The one exception is
  `/adjustbalance <delta>` (e.g. `/adjustbalance -1135.89`), or asking
  naturally ("add this to my rolled-over deficit -1135.89", "adjust my
  balance by -50") — a manual nudge for when the derivation itself can't be
  trusted, most commonly after losing expense history from before some date
  (a redeploy without persistent storage, see "Deploy to Railway" below): the
  automatic rollover has nothing to derive that period's real deficit from,
  so it rolls those days over as if $0 was spent, understating a real
  overspend. `/adjustbalance` takes a signed delta (added to the current
  balance), not a replacement value, and reverses with one-word `undo` like
  every other correction.
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
- **Advisor-style insights, not just category totals**: each category's total
  is paired with a real historical baseline (its average over the several
  periods before this one) and the actual largest individual transactions in
  the period. That's what lets `/summary` tell a one-off occasion (several
  wedding ang pows this month) from a genuine behavioral spike (a hobby
  category running well above its usual rate), and name the specific
  transaction actually driving a category total instead of just reporting the
  sum — see "Usage" below for what that looks like in practice.

## How meal and workout logging works

- **Meals are logged as items, not meal slots.** Send "coke zero and 750ml
  water" or a photo of your dinner and it's logged as-is — there's no
  requirement to file it under breakfast/lunch/dinner/snack (`meal_type` is
  set when it's obvious, left blank otherwise).
- **Calories are a range, not a false-precise number.** Every meal gets a
  `calories_low`–`calories_high` estimate plus a central `calories_estimate`
  (what running totals sum), the same way a careful human estimate would be
  given — never a single suspiciously exact figure.
- **Photos work directly** — send a picture of your food, with or without a
  caption, and no command needed; Claude estimates portions from the image
  the same way it estimates from a text description, refining with the
  caption when there is one.
- **A photo isn't assumed to be food — it's classified first.** A screenshot
  of a fitness app or wearable's daily stats (calories burned, steps, active
  minutes) is recognized as that, not force-fit into a fake meal, and gets
  logged as a **workout** instead, with `calories_burned` set. Anything else
  (a receipt, a random photo) isn't logged at all — Morrow asks what you
  actually meant rather than guessing.
- **A caption naming a different food than the photo doesn't get blended
  in.** Send a photo of one dish with a caption like "I also had a small
  bowl of soup" and Morrow won't merge them into one entry with combined
  (wrong) calories — it asks which food(s) you actually want logged, and
  your plain-text answer resolves it, exactly like answering any other
  clarifying question.
- **An explicit caption is trusted over a visual guess at what's on the
  plate.** If you name the specific dish ("medium curry gyu don from
  Sukiya"), Morrow lists what you actually said you ate, refined by what's
  visible in the photo — it won't pad the item list with extra components
  (a side, a garnish) it can't actually confirm are really there just
  because they're commonly served alongside that dish. Guessing an item in
  is treated as worse than leaving one out, since an uninvited extra item
  silently inflates the calorie log.
- **Water is tracked separately from calories.** Mention plain water (e.g.
  "750ml water") and it adds to a running `water_ml` total for the day,
  never estimated for other drinks.
- Every meal/photo reply includes the day's running calorie (and water, if
  any) total, not just the item just logged.
- **More than one meal in a single message is logged as separate meals**,
  not merged or dropped. "For breakfast: toast and coffee. For lunch:
  noodles and a latte" logs two entries (breakfast and lunch, each with its
  own calorie range) and replies with both, the same way "$5 for lunch and
  $5 for coffee" logs two separate expenses instead of one.
- **Workouts** (`/logworkout` or natural language, e.g. "played tennis for an
  hour, won 2 sets") capture activity, duration, distance when mentioned, and
  free-text notes for anything else worth keeping (splits, sets, how it felt)
  rather than forcing structure that isn't there yet.
- **Calories burned are shown against calories eaten.** When a workout is
  logged with `calories_burned` set (typically from a fitness app/wearable
  screenshot — see above), the reply also shows that day's running "calories
  in vs calories burned" balance, not just the number in isolation.
- **Two photos of the same fitness-app data don't get logged as two
  workouts.** A real bad interaction: two screenshots of the same day's
  stats (a Move-goal screen and an Activity-summary screen) reported the
  same `calories_burned` and got logged as two separate workouts, silently
  doubling the day's burned total. A new photo whose `calories_burned`
  closely matches (within 2%, minimum 5 kcal) an already-logged workout for
  the same day is flagged as a likely duplicate — Morrow asks whether it's
  really a separate workout or the same one shown again, rather than
  logging it twice automatically.
- **A photo's caption naming a specific past day backdates the log
  immediately, at log time.** "These were my stats for 15 September" used
  to get logged as today anyway, needing a manual `/undo` plus a date
  correction afterwards. Claude now extracts how many days ago the caption
  implies (never an actual date — the bot converts that day-count into a
  real date deterministically, the same discipline used everywhere else a
  date is involved) and logs the meal or workout directly onto that day.
- **Vitals** (`/logvitals` or natural language, e.g. "weight 76.6, slept 5.5
  hours, knee 2/10") log whatever you mention — weight, sleep hours, knee
  pain (0-10), and free-text notes — leaving anything you didn't mention
  blank rather than forcing every check-in to be complete.
- **Corrections** work the same way as expenses for the common cases — "that
  meal was actually two days ago" or "delete that, I logged it twice" both
  resolve by natural language, with the same before/after confirmation and
  one-word `undo`. This applies to meals, workouts, and vitals check-ins too.
  Field-level edits (fixing an activity name or a vitals reading) aren't
  wired up yet for workouts/vitals — only moving the date or deleting an
  entry — so a request for anything else gets a clarifying message rather
  than being silently ignored.
- **Meals get one field-level edit that workouts/vitals don't yet: correcting
  what was actually eaten**, without deleting and relogging from scratch —
  e.g. "minus the ramen noodles, I didn't have that" or "I also had a side
  salad". Morrow re-estimates calories fresh for the corrected item list
  (the same plausible-range discipline as a brand-new log) and applies it in
  one shot; reverses with one-word `undo` like every other correction. This
  exists specifically because a photo-logged meal is the one place a
  hallucinated extra item (see above) is most likely to slip in, and
  deleting the whole entry to fix one wrong item was real, reported
  friction.

## How memory works

Morrow keeps two separate, deliberately different kinds of memory (see
`db.py`'s module docstring for the schema rationale):

- **Rolling conversation history** (`messages` table) — every free-text turn,
  both yours and Morrow's, logged automatically. The last ~30 turns are fed
  into every AI call so the thread stays in context — "that", "it", and
  follow-ups resolve the way they would in an ongoing chat, not a fresh form
  each time. Nothing to manage here; it just accumulates. Slash commands
  (`/log`, `/balance`, etc.) aren't logged to this yet, only natural-language
  messages.
- **Durable memory** (`memory` table) — standing goals, plans, and
  preferences you explicitly ask Morrow to remember, e.g. "remember I go to
  Fitness First Bugis Tue/Thu for legs and back" or "remember my goal is
  75kg by December". Each saved item has a short label, an optional
  category, and the actual content. Mention something later ("what's my
  split today?") and Morrow can answer directly from the saved content — no
  separate lookup step, since the full list is read into every call. Saying
  "remember X" again about the same thing updates that label in place rather
  than creating a duplicate.
  - See everything saved: `/memory`, or ask naturally ("what do you
    remember about me?").
  - Remove something: `/forget <label>`, or naturally ("forget the Bugis gym
    plan") — reverses with one-word `undo`, same as other corrections.

## How the rundown works

`/rundown`, or asking naturally ("how am I doing", "how's my week been",
"give me a rundown"), answers across all four domains at once instead of
just one. Unlike a `casual` reply, it doesn't let Claude guess at numbers
from the short recent-items lists already in context — `bot.py` computes
real 7-day figures first (today's balance status, meal count/calories/
water, workout count/activities, vitals check-ins/latest weight/weight
change/average sleep/average knee pain), and only hands those real numbers
to Claude to narrate into a few short lines. A domain with nothing logged
that week is left out rather than reported as a misleading zero. If the
Claude call itself fails, it falls back to a plain, deterministic breakdown
of the same numbers — never silent, same discipline as `/summary`.

That's the dividing line from `/balance` and `/recent`: those stay
expense-only and answer *today's* numbers directly; `/rundown` is the one
that reads across money, food, training, and vitals together over the
last week.

## How the morning briefing works

Every day at a fixed local time (`MORNING_BRIEFING_HOUR`/`MORNING_BRIEFING_MINUTE`
in `BOT_TIMEZONE`, default 7:30am), Morrow pushes a short digest to every
chat automatically — no command needed. `/morning` shows the exact same
content on demand, any time, so you can check it works or re-check today's
digest later in the day.

Unlike `/rundown`'s 7-day retrospective, the briefing is forward-looking:
today's target, rolled-over balance, and streak; any open to-do that's due
today or already overdue (an undated to-do, or one due later this week,
isn't something that needs attention *this morning* specifically — `/tasks`
still shows the full list); any daily reminder not yet checked off today
(see "How daily reminders work" below — it drops off this list the moment
it's marked done for the day, and comes back on its own tomorrow with no
separate reset job); what's coming up on your schedule over the next 7 days
(see "How scheduled events work" below — a focused near-term window, not the
full list `/events` shows); and, for light context, a one-line look back at
just yesterday (calories eaten, workouts, vitals check-ins). There's no AI
narration step here at all — every figure is computed directly the same
"never let the model guess a number" way as `/rundown`, but skipping the
Claude call entirely means the one message that's supposed to show up
reliably every single morning can never be delayed or broken by an API
hiccup.

If `python-telegram-bot`'s job-queue extra isn't installed, the automatic
daily push won't fire (same caveat as the hourly rollover check — see the
startup log warning), but `/morning` still works on demand regardless.

## How to-dos work

- **Add one** with `/addtask <description>` or naturally — "remind me to call
  the dentist tomorrow 5pm", "add buy milk to my list", "need to submit the
  report by friday". A due date/time is only set if you actually mention one;
  otherwise the to-do just sits on the list with no deadline.
- **A whole list of to-dos in one message is added as separate to-dos**, not
  merged or dropped. A numbered list of 13 different things to do logs 13
  to-dos, each with its own title and (if mentioned) its own due date, the
  same way "$5 for lunch and $5 for coffee" logs two separate expenses.
- **Due dates are computed in code, never guessed by Claude** — same
  discipline as expense date corrections. The model only ever extracts *how
  many days from today* (and a clock time, if one was mentioned); Morrow
  turns that into an actual calendar date.
- **See what's open** with `/tasks`, or ask naturally ("what's on my list?",
  "what do I need to do?") — soonest-due-first, undated to-dos last, done
  ones left off the list entirely.
- **Mark one done** with `/done <id>` or naturally ("I finished calling the
  dentist", "mark the dentist call as done") — reverses with one-word `undo`,
  same as other corrections.
- **Edit a to-do, not just its due date** — mark one done, delete one, or
  edit its title, due date, and/or notes, all through one flexible
  correction that changes whichever fields the message actually implies
  (e.g. "push #11 to tomorrow" reschedules only; "push #11 to tomorrow, I
  need Shardul's address" reschedules AND adds a note, in one go). A due
  date here is forward-looking — the model extracts *how many days from
  today*, not a backward "N days ago" like other domains' date corrections,
  since a due date can legitimately move into the future. Reverses with
  one-word `undo` like every other correction, restoring the exact prior
  title/due date/notes.

## How daily reminders work

- **Not the same thing as a to-do.** A to-do (`/addtask`) is one-off — done
  once, checked off for good. A daily reminder (`/addreminder`) is a standing
  habit (e.g. "take hair pills", "stretch before bed") that resurfaces every
  single day, forever, until you remove it — nothing ever marks it "done"
  permanently.
- **Add one** with `/addreminder <description>` or naturally — "remind me
  every day to take my hair pills", "add a daily reminder to stretch before
  bed". The giveaway phrase is "every day"/"daily"/"each day"; a message with
  an actual deadline instead ("tomorrow", "by friday") is a to-do, not this.
- **Shows up in your morning briefing automatically**, every day, until it's
  either checked off for that day or removed — see "How the morning briefing
  works" above. `/reminders` shows the full standing list any time (including
  ones already done today, tagged `[done today]`, so it stays a stable
  reference of everything you've set, not just what's left).
- **Mark one done for today** with `/donereminder <id>` — it comes right
  back tomorrow; this only checks off *today's* occurrence, not the
  reminder itself. **Remove one for good** with `/removereminder <id>`.
  Both reverse with one-word `undo`, same as every other correction.
- **Marking done or removing is command-only right now** (`/donereminder
  <id>` / `/removereminder <id>`), not yet natural language — adding one and
  viewing the list both work conversationally, but resolving an existing
  reminder by name in free text (rather than by id) isn't wired up yet. Come
  back to this if it turns out to matter in practice.
- **No separate daily reset job needed.** "Done today" is just a stored date
  compared against today's real date (BOT_TIMEZONE-aware, see the day-boundary
  note above) every time it's read — so a reminder checked off yesterday is
  automatically pending again today, with nothing needing to run at midnight.

## How scheduled events work

- **Not a to-do, and not a daily reminder.** A to-do (`/addtask`) gets
  actively done and checked off; a daily reminder (`/addreminder`) recurs
  forever. A scheduled event (`/addevent`) is a flat, one-off, dated
  appointment — "Dinner with Mel next Monday", "Company Tennis Wednesday at
  6pm" — that just happens on its day. There's no "done" state to mark; it
  simply drops off the upcoming list once its date has passed.
- **Add one** with `/addevent <description>` or naturally — "dinner with Mel
  next Monday", "I have a dentist appointment Thursday at 3pm". A day is
  required (Morrow asks if it can't tell which day you mean); a clock time is
  only set if you actually mentioned one.
- **A whole batch in one message is added as separate events**, not merged or
  dropped — the same discipline as a numbered list of to-dos. This is what
  makes committing a week's workout plan work in one shot: propose a plan
  conversationally, then say "log that into my schedule for the week" and
  each day becomes its own event.
- **Event dates are computed in code, never guessed by Claude** — same
  discipline as to-do due dates. The model only ever extracts *how many days
  from today* (and a clock time, if mentioned); Morrow turns that into an
  actual calendar date.
- **See what's upcoming** with `/events`, or ask naturally ("what's on my
  schedule", "what's coming up") — soonest first, past events left off
  entirely (there's nothing left to show for something that already happened).
- **Clear one by natural language once it's happened or is no longer
  relevant** — "the X-ray is done", "cancel dinner with Mel", "that
  appointment got moved, just remove it for now" all resolve the same way as
  any other correction, matched against your upcoming events by id or title,
  with the usual before/after confirmation and one-word `undo`. An event has
  no "done" state (see above), so "X is done" here means the same thing as
  "delete X" — there's nothing to mark complete, just something to take off
  the list. `/removeevent <id>` does the same thing as a command.
- **Rescheduling is command-only right now**: `/rescheduleevent <id> <days
  from today>` — reverses with one-word `undo` like everything else. Natural
  language handles adding, viewing, and clearing an event, but moving one to
  a new day by free text isn't wired up yet (a correction asking to
  reschedule gets a clarifying nudge toward the command instead of silently
  doing nothing). Come back to this if it turns out to matter in practice.
- **Deliberately flat, not truly recurring.** There's no "every Wednesday"
  rule here — a real recurring event needs day-of-week/interval matching plus
  the classic "edit one occurrence vs. all future occurrences" problem,
  neither of which this codebase has an established pattern for yet. The
  actual near-term driver (a week's workout plan) is really just several
  flat dated rows regenerated by a fresh planning conversation each week, not
  a true recurring rule, so this covers it without guessing at a recurrence
  design ahead of a real driving case.

## Files

| File | Purpose |
|---|---|
| `bot.py` | Thin facade re-exporting every domain module's names (see its own docstring) |
| `app.py` | Entrypoint: builds the bot, registers every handler, the periodic rollover job |
| `morning.py` | The daily morning briefing (`/morning` + the automatic proactive push) |
| `reminders.py` | Daily recurring reminders (`/addreminder`, `/reminders`, `/donereminder`, `/removereminder`) |
| `events.py` | Flat scheduled events/appointments (`/addevent`, `/events`, `/rescheduleevent`, `/removeevent`) |
| `db.py` | SQLite schema + all balance/rollover/claimable/streak/alert logic |
| `fx.py` | Currency conversion (Frankfurter/ECB rates, cached, with 1:1 fallback) |
| `ai.py` | Claude calls: natural-language parsing, categorization, on-demand summaries |
| `trends.py` | Pure period-boundary math for `/summary`'s trend comparisons (no framework deps, unit tested directly) |
| `config.py` | Reads all settings from environment variables |
| `requirements.txt` | Python dependencies |
| `requirements-dev.txt` | Adds `pytest` for running the test suite |
| `tests/` | Unit tests (`pytest`) covering rollover/streak/currency math, trend boundaries, and AI-call resilience |
| `Procfile` | Tells Railway how to run the bot as a background worker |
| `.env.example` | Template for the environment variables you need to set |

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

Tests use a throwaway SQLite file per test and mock both the Frankfurter FX
API and the Anthropic client, so the whole suite runs offline in under a
second with no API costs. `.github/workflows/tests.yml` runs it automatically
on every push.

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
/adjustbalance <delta>            manually nudge rolled-over balance, e.g. /adjustbalance -1135.89

/logmeal <description>           log food/drink, e.g. /logmeal chicken rice and iced tea
                                  (or just send a photo of your food, caption optional)
/recentmeals [n]                 last n logged meals (default 10)
/logworkout <description>        log a workout, e.g. /logworkout tennis for an hour
/recentworkouts [n]              last n logged workouts (default 10)
/logvitals <description>         log a check-in, e.g. /logvitals weight 76.6, knee 2/10
/recentvitals [n]                last n logged check-ins (default 10)

/memory                          list everything currently remembered
/forget <label>                  remove a remembered item (see /memory for exact labels)

/addtask <description>            add a to-do, e.g. /addtask call the dentist tomorrow 5pm
/tasks                            show the open to-do list, soonest due first
/done <id>                        mark a to-do done

/addreminder <description>        add a DAILY recurring reminder, e.g. /addreminder take hair pills
/reminders                        show all daily reminders (done-today ones tagged)
/donereminder <id>                mark a daily reminder done for today only -- comes back tomorrow
/removereminder <id>              remove a daily reminder for good

/addevent <description>           add a one-off scheduled event, e.g. /addevent dinner with Mel next Monday
/events                           show what's coming up, soonest first
/rescheduleevent <id> <days>      move an event to a new day, e.g. /rescheduleevent 3 1 for tomorrow
/removeevent <id>                 remove a scheduled event

/rundown                         cross-domain check-in: money + food + training + vitals, last 7 days
/morning                         today's briefing: budget + due/overdue to-dos + reminders + what's coming
                                  up + a look back at yesterday (also sent automatically once a day -- see
                                  "How the morning briefing works")
```

Or skip commands and just type naturally:

> spent 15 on uber
> paid 300 for the team dinner, claimable
> got groceries for 84 bucks
> spent 20 USD on a hotel
> coke zero and 750ml water
> had a mango
> played tennis for an hour, won 2 sets
> did IPPT training, ran 2.4km in 10:45
> weight 76.6, slept 5.5 hours, knee 2/10
> remember I go to Fitness First Bugis Tue/Thu for legs and back
> forget the Bugis gym plan
> what's my split today? (answered from what's already remembered, no lookup command needed)
> remind me to call the dentist tomorrow 5pm
> what's on my list?
> I finished calling the dentist
> dinner with Mel next Monday
> what's on my schedule?
> how am I doing this week? (a real cross-domain check-in, not a guess)

And beyond logging/remembering, just talk to it — Morrow keeps the last
stretch of the conversation in context (see "How memory works" above), so
it's not limited to boxed intents; it can hold up its end of an open,
back-and-forth chat.

The bot asks a quick follow-up only when it's genuinely unsure (missing
amount, ambiguous category, or a large charge with no claimable hint). It
only asks about currency if you never mention one — otherwise it defaults to
your base currency. A bare currency symbol with no letters (e.g. just "$")
also defaults to your base currency rather than being assumed to mean USD.

You can also correct something you already logged just by talking about it,
without needing the expense's ID:

> that was for yesterday, not today
> no, my lunch was in SGD not USD
> you double logged my lunch, delete one
> that log from yesterday was wrong, tag it to the day before instead
> minus the ramen noodles, I didn't have that
> the X-ray is done, take it off my schedule

The bot looks at your last few logged expenses, figures out which one you
mean — by amount, description, or a date reference alone (e.g. "yesterday's
log" is enough on its own if only one recent expense was logged then) — and
applies the fix: changing the date (including "3 days ago", not just
today/yesterday), currency, amount, description, category, or deleting it
outright. Every one of those replies with a real before/after confirmation
built from what's actually in the database (never a made-up "done!" — that
was a real bug this fixed), and the very next message can be a bare `undo`,
`no`, or `wrong` to revert exactly that one change. If it can't confidently
tell which of your recent expenses you mean, it asks rather than guessing —
pointing you at `/recent` to get an ID for `/edit`/`/delete` if needed. This
only looks at roughly your last 8 expenses; older corrections still need the
explicit commands. If a message describes more than one correction at once,
it resolves the clearer one and you can follow up separately for the other.

Asking to see your balance or recent expenses in plain English works too —
"show me my balance" or "what have I logged today" answers immediately with
the real numbers, the same as `/balance`/`/recent` would, rather than just
telling you to go type the command.

Undo/edit/delete (including natural-language corrections) are balance-safe:
if you correct an expense logged on a previous day (already rolled into your
balance), the balance is adjusted retroactively so the numbers stay
accurate. Same-day corrections don't need any adjustment since today's spend
is always computed live.

Two things the bot deliberately never does: it never claims to have
performed an edit, deletion, or "note" unless it actually called the
underlying database function first (its casual chat replies are talk-only
and can't represent an action having been taken), and it never denies a
capability that actually exists (e.g. claiming it can't show your balance or
recent expenses) — it knows its own real command list and will point you at
the right one instead of inventing a limitation.

`/summary` is entirely on-demand — nothing about trends or patterns is ever
pushed to you unprompted. Ask for it and it'll surface whatever's actually
worth knowing: how this period compares to the last one, which category moved
the most, whether a particular day of the week tends to be pricier, and how
many days you stayed under target. Thin or trivial signals (e.g. a change
under ~10%) are left out rather than padding the message.

It reasons about *why* a category moved, not just that it did. Four wedding
ang pows this month reads as "Gifts & Occasions is up, but that's an
unpredictable one-off category — without it you'd be well within budget,"
rather than a flat "you spent more on Gifts." A real 15x jump on a recurring
hobby category reads as "you've used up the month's budget on Pokémon cards
— a real spike from your usual ~$200/month, worth a look if nothing's
changed financially" — naming the actual multiple and the actual typical
amount, not a vague "spend less." Both come from a real historical baseline
per category and the actual largest transactions in the period, computed in
Python and handed to Claude to narrate — never estimated by the model itself.

## Notes / limitations

- This is built for a single primary user (you), though the schema supports
  multiple chats if you ever want to share it.
- `BOT_TIMEZONE` and `BASE_CURRENCY` are global settings (not per-user) —
  simplest correct choice for a personal bot.
- **Every "what day is it" computation goes through `db.today_str()`**
  (or fx.py's own identical, independently-monkeypatchable twin,
  `fx._now_local_date()` — see its docstring for why it can't just import
  `db.py`), never the bare `date.today()`/`datetime.now()`. This mattered
  in practice: Railway's server clock is UTC, and Asia/Singapore is
  UTC+8, so `date.today()` lags the real Singapore calendar date by up to
  8 hours after local midnight — logging, backdating a photo, `/summary
  today`, and the exchange-rate cache all used to disagree with each
  other (and with what you actually meant) in that window. If you add a
  new date computation anywhere, reach for `db.today_str()`.
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
- The rolling conversation history only captures natural-language messages
  through `handle_text`, not slash commands (`/log`, `/balance`, etc.) — so
  a reply from `/logmeal` is logged (Morrow's side only), but the `/logmeal`
  command itself isn't. Worth revisiting if command usage turns out to
  matter for later context.
- Durable memory has no size cap or automatic pruning yet — fine at personal
  scale, but if the list grows very large it may eventually be worth
  trimming or summarizing rather than reading the whole thing into every
  prompt.
