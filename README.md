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
- **Every workout confirmation also shows the trailing 7-day count** (and
  total calories burned, when reported) — "This week: 3 workouts logged,
  ~1,200 kcal burned" — so logging one workout also shows how the week's
  shaping up, not just a bare "Logged: ...". All real, deterministically
  computed from the database, no AI call involved (same discipline as the
  daily meal totals above) — this applies to `/logworkout`, photo logging,
  and the natural-language path alike.
- **A structured lift shows a same-exercise comparison when there's a prior
  one to compare against** — "Last time (3 days ago): 30kg x8, 30kg x8,
  32kg x6" alongside the fresh confirmation, matched by exercise name
  case-insensitively. A brand-new exercise, or the first time it's ever been
  logged, just gets the plain confirmation — there's nothing to compare yet.
- **A vitals check-in shows the delta against the previous one** — "Since
  last check-in: weight -0.4kg vs last (2024-01-03), sleep +1.0h" — only for
  fields both check-ins actually reported, so a partial check-in never gets
  a misleading comparison against something it didn't mention. A first-ever
  check-in gets no trend line, since there's nothing prior yet.
- **Two photos of the same fitness-app data don't get logged as two
  workouts.** A real bad interaction: two screenshots of the same day's
  stats (a Move-goal screen and an Activity-summary screen) reported the
  same `calories_burned` and got logged as two separate workouts, silently
  doubling the day's burned total. A new photo whose `calories_burned`
  closely matches (within 2%, minimum 5 kcal) an already-logged workout for
  the same day is flagged as a likely duplicate — Morrow asks whether it's
  really a separate workout or the same one shown again, rather than
  logging it twice automatically.
- **Several photos sent together in one message are read together, not one at a
  time.** Telegram delivers a multi-photo message ("album") as a separate
  update per photo, usually with the caption attached to only one of them -- a
  real reported bug: two screenshots of the same day's fitness data (e.g. an
  activity-rings detail screen plus a second screen restating part of the same
  totals) got analyzed independently, with no way for either call to know about
  the other, and landed as two separate workouts -- one of them even on the
  wrong date, since it had no caption of its own to backdate from. Morrow now
  buffers every photo that shares the same album briefly (waiting for a short
  pause after the last one before assuming the album is complete), then hands
  the whole set to Claude in one call so it can recognize "these are the same
  underlying day" instead of double-counting. A photo sent on its own is
  completely unaffected.
- **A caption (or, for a typed/natural-language message, the message itself)
  naming a specific past day backdates the log immediately, at log time.**
  "These were my stats for 15 September" or "last night I also had a cup of
  tea" used to get logged as today anyway, needing a manual `/undo` plus a date
  correction afterwards -- this applied to a caption's own day reference, and,
  until now, wasn't even possible for a plain natural-language log (an expense,
  meal, workout, or vitals check-in) with no photo involved at all. Claude now
  extracts how many days ago is meant (never an actual date -- the bot converts
  that day-count into a real date deterministically, the same discipline used
  everywhere else a date is involved) and logs directly onto that day, for any
  of the four loggable domains. A message naming more than one day at once
  (rare, but "yesterday I had a mango, and just now a coffee") logs each item
  onto its own day and shows a running total for each day actually touched, not
  just one.
- **Vitals** (`/logvitals` or natural language, e.g. "weight 76.6, slept 5.5
  hours, knee 2/10") log whatever you mention — weight, sleep hours, knee
  pain (0-10), and free-text notes — leaving anything you didn't mention
  blank rather than forcing every check-in to be complete.
- **Corrections** work the same way as expenses for the common cases — "that
  meal was actually two days ago" or "delete that, I logged it twice" both
  resolve by natural language, with the same before/after confirmation and
  one-word `undo`. This applies to meals, workouts, lifts, and vitals
  check-ins too. A request for a field genuinely outside what's supported
  for a domain (currently just an event's title/notes, since events only
  support moving the day or deleting) gets a clear "that kind of edit isn't
  supported yet" message naming what actually is, rather than a confusing
  off-topic question or being silently ignored.
- **Meals, workouts, lifts, and vitals check-ins each get a field-level
  edit**, correcting what's actually in an already-logged entry without
  deleting and relogging from scratch:
  - Meals: what was actually eaten/drunk — e.g. "minus the ramen noodles, I
    didn't have that" or "I also had a side salad". Morrow re-estimates
    calories fresh for the corrected item list (the same plausible-range
    discipline as a brand-new log) and applies it in one shot. This exists
    specifically because a photo-logged meal is the one place a
    hallucinated extra item (see above) is most likely to slip in, and
    deleting the whole entry to fix one wrong item was real, reported
    friction.
  - Workouts: activity, duration, distance, calories burned, or notes —
    e.g. "correct the calories out to 2862" when a photo-read total (say,
    a specific workout's burn) turns out to only be part of the real
    number a tracker later showed (the full day's total including basal
    metabolic rate).
  - Lifts: sets, location, effort, or notes — e.g. "the v bar rows were
    actually 10x8x1 and 12x8x2", the same "give me the whole corrected
    picture, not just the diff" discipline as a meal's item-list
    correction, not a delete-and-relog round trip for a mis-typed rep
    count.
  - Vitals: weight, sleep hours, knee pain, or notes — e.g. "that was
    76.0kg not 76.6" or "I actually slept 7 hours, not 5.5" — fixing a
    mis-typed number on a check-in without deleting and relogging it.
  All four reverse with one-word `undo` like every other correction.

## How structured lift logging works

- **Lifts are logged per exercise, not per session.** `workouts` stays a
  single free-text blob per session (cardio, tennis, a fitness-app
  daily-activity summary) — a gym day with actual sets/reps/weight data goes
  through a separate `lifts` domain instead, one row per exercise: "pull-ups
  10x3, then v-bar rows 35kg 8x3, then lat pulldown 70kg 8x3" in one message
  logs three rows, the same "one row per item" discipline as meals/tasks.
  Morrow decides which domain a message belongs to based on whether real
  set/rep/weight detail was actually given — "gym, legs day" on its own
  stays a `log_workout`; "squat 90kg x5x3, felt strong" is a `log_lift`.
- **A described completed session always logs as `log_lift`, never
  `remember`, even right after Morrow itself used the word "remember."**
  Asking "want me to remember your Visa pull day routine going forward?"
  and then answering with the real numbers you just did is still a session
  that happened — it logs for real, the same as if you'd led with it
  unprompted. This closes a real bug where Morrow's own prior wording
  primed the model into filing an actual workout as a memory note instead
  of a queryable, correctable logged session.
- **Each exercise carries its own location** (e.g. "AF Wheelock", "office
  gym", "Capella"), kept as free text rather than a fixed list — your gym
  vocabulary shifts over time, and different locations/machines aren't
  directly comparable numbers anyway.
- **A set's load is always a string, never forced into a number** — a real
  weight with its unit ("35kg") and a numbered-machine setting ("setting
  21") are both valid loads, and conflating them into one unit would be
  actively wrong for an uncalibrated machine.
- **Effort and context notes are free text, only set when actually said** —
  "felt strong", "grindy last set", "no warm-up set", "trained again the
  next day" — the kind of detail that matters when comparing a session to a
  future one, captured rather than discarded.
- `/loglift <description>` logs one exercise by command (e.g. `/loglift
  bench press 80kg 4x3 at biopolis`); `/recentlifts [n]` lists recent ones.
  Corrections via natural language cover moving the date, correcting the
  sets/location/effort/notes (see "How meal and workout logging works"
  above), or deleting one — each with one-word `undo`.
- **This is Phase A of a larger coaching-engine plan, not the whole
  thing.** Nothing reads this data to give a verdict, compare it against a
  progression target, or prescribe next-session numbers yet — right now
  it's purely about getting real per-(exercise, location) data flowing into
  structured rows instead of vanishing into `workouts.activity` free text.
  The coaching layer (comparing a set against history, a next-target table,
  a maintenance-vs-progression mode) is a deliberately separate, later
  phase — see the project's coaching-engine planning notes.

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

## How day stats works

`/daystats [today|yesterday|N]`, or asking naturally about ONE specific day
("stats from yesterday", "what did I eat Saturday", "calories for the
20th", or a bare "show me the stats again" following an earlier stats
question), answers with real, freshly re-read numbers for exactly that
day — meals eaten (with the actual item names, not just a total), water,
workouts and calories burned, net calories, and that day's vitals
check-in if there is one. Same "real numbers in, never guessed" discipline
as `/rundown`, just scoped to one day instead of a trailing week.

This exists specifically because a single-day calorie question used to
fall through to a plain `casual` reply, where Claude was given the last
~10 raw meals/workouts (not even filtered to the day being asked about)
and asked to filter, sum, and subtract them itself, in freeform text, from
scratch, on every single ask. In practice that produced a different (and
sometimes internally contradictory, even sign-flipped) answer each time
the same question was re-asked, and once backfilled a fabricated "I
corrected that entry" explanation for its own inconsistency instead of
just re-reading the database. `day_stats` guarantees an identical, real DB
read backs the answer every time instead — if the user disputed a number
earlier in the conversation, this fresh read is the one that's actually
right, and the model is told explicitly not to "correct" it based on
anything said earlier.

`rundown` and `day_stats` are deliberately split on ONE axis: whether the
message names or implies a specific day. A specific day (even one that
also spans multiple domains, e.g. "how'd I eat and train yesterday") is
`day_stats`; an open-ended, no-particular-day, multi-day question ("how's
my week been") is `rundown`.

## How casual conversation works

Anything that isn't logging, correcting, checking a real number, or
remembering something durable — small talk, catching up, venting, asking
for advice, or a specific question answerable from context/memory — gets
classified as the `casual` intent and answered by a **dedicated Claude
call** (`ai.answer_casually`), not by `parse_message` itself.

This is a deliberate split, not just an implementation detail. Every other
intent (`log_meal`, `correction`, `day_stats`, …) needs `parse_message` to
correctly classify the message AND extract a pile of structured fields, all
in one JSON-constrained completion — there's no room in that same call to
also write a genuinely warm, engaged conversational reply without the two
jobs competing for the model's attention. `answer_casually` has nothing to
extract and nothing to return but plain text, so it gets its own call, with
real room to actually converse, fed:

- the rolling conversation history and full durable memory (the same two
  things `parse_message` already sees), so it stays in the actual thread
  instead of treating every message as a fresh start, and
- a `today_snapshot` — today's real balance/streak and today's real
  meals/workouts/vitals so far (the same deterministically-computed figures
  `day_stats` trusts), so it can actually converse with knowledge of how
  today's going ("nothing logged yet today, quiet one so far") instead of
  talking in a vacuum, without ever recomputing or guessing at a number
  itself, and
- `recent_lifts` — real logged gym-exercise rows (exercise, location, sets,
  effort, notes, date), most recent first. This grounds any gym-routine
  question ("what's my push day at Visa look like") in actual logged data
  rather than letting the model freely narrate from durable-memory prose —
  fixing a real bug where the exact sets/reps/weight quoted back would
  drift between two near-identical questions asked minutes apart in the
  same conversation. If a specific session simply isn't in the recent-lifts
  data, Morrow says so rather than reconstructing one from context.

It also uses a separate model setting, `CLAUDE_NARRATION_MODEL` (an
optional env var, defaults to whatever `CLAUDE_MODEL` is set to — see
`config.py`) — writing a good conversational reply benefits from a
stronger model even though fast structured extraction doesn't need one,
and this way you only pay for that on the calls that actually need it. Set
it to a Sonnet-tier model in your `.env` for noticeably better conversation
and narration without paying that cost on every single expense/meal
logged. The same setting is also used for `/rundown` and `/daystats`'s
narration, and `/summary`'s trend write-up.

If the dedicated call itself fails for some reason (API hiccup, rate
limit), Morrow falls back to `parse_message`'s own `casual_reply` field —
the same "never go silent" discipline as `/rundown`/`/daystats` falling
back to a plain deterministic rendering.

Same guardrail as always: this call only talks, and can't write anything to
the database or queue up a future action — it's told explicitly never to
claim it logged, corrected, remembered, or will "look into" anything, and
to say plainly that you should just say the thing as its own message (or
use the matching command) if you actually want it done.

If the model doesn't classify a message as *any* known intent — not even
`casual` — that's treated as a real gap, not conversation: you get the
plain command-menu fallback instead, so a genuine misclassification isn't
quietly answered as if it were small talk.

## How Morrow's companion voice works on every reply, not just casual chat

`answer_casually` gave open-ended conversation real personality, but for a
long time everything else — a log confirmation, a correction, a clarifying
question, an "I didn't catch that" — stayed a flat, hard-coded string. That
turned out to be most of what Morrow actually says day to day, so logging
still read like a receipt printer even after `answer_casually` shipped:
the companion voice only ever fired for the minority of messages
classified as pure small talk.

`ai.narrate_reply` closes that gap. Every reply sent on the free-text/
command surface passes through exactly one chokepoint, `replies._reply`,
and by default (`narrate=True`) that chokepoint now runs the reply through
`narrate_reply` before it goes out — so `"Logged: pull-ups 10x3"` comes
back sounding like an actual companion responding, not a bot printing a
line. Critically, this is a **restyle, not a rewrite**: the content (every
number, id, date, and fact) is already correct and final by the time this
call runs — computed the exact same deterministic way it always was —
and `narrate_reply`'s only job is delivery. It's told explicitly to never
drop, round, or invent a number/id/fact, never drop an actionable
instruction like "Reply 'undo' if that's wrong", and to keep a plain log
short (a line or two) rather than padding it into a paragraph. It's fed
the same grounding context as `answer_casually` (conversation history,
memory, `today_snapshot`, `recent_lifts`) so a confirmation can actually
reference something relevant instead of reading like a stateless template.

A handful of replies are already full narrations in their own right —
`answer_casually` (`casual`), `answer_with_rundown` (`rundown`),
`answer_with_day_stats` (`day_stats`) — and those are sent with
`narrate=False` so they're not run through a second, redundant pass that
could quietly drift from the first pass's real numbers. Everything else —
every domain module's log/correction/undo/clarification replies, and the
`show_balance`/`show_recent`/`show_tasks`/`show_events`/`show_reminders`/
`show_memory` intents — gets the companion treatment automatically, since
they all already funnel through `_reply` with no code changes needed at
each call site. The morning briefing and evening nudge (see their own
sections below) have no `Update` to reply to — they're pushed by a
schedule, not sent in response to a message — so they go through
`replies._send_proactive` instead, `_reply`'s sibling for exactly that
case, with the identical narrate-by-default/fallback/conversation-history
behavior.

If `narrate_reply` itself fails (API hiccup, rate limit), `_reply`/
`_send_proactive` fall back to sending the original deterministic text
unchanged — same "never go silent, never say something false" discipline
as every other narration call in this codebase. This does mean essentially
every reply now costs one extra `CLAUDE_NARRATION_MODEL` call (a bit more
latency, a small ongoing API cost) — a deliberate tradeoff for the bot
actually feeling like a companion at all times rather than only during
open-ended chat.

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
synthesis step deciding what the briefing says — every figure is computed
directly the same "never let the model guess a number" way as `/rundown`,
so the content itself can never be delayed or broken by an API hiccup.

It IS, like every other reply, run through Morrow's companion voice
(`ai.narrate_reply`, see "How Morrow's companion voice works on every
reply" above) before it's actually sent — a pure restyle of this
already-correct text, never a re-synthesis, with the same
fall-back-to-the-plain-text safety net, so a narration hiccup just means
the briefing goes out in its plain deterministic form instead of being
lost or delayed.

If `python-telegram-bot`'s job-queue extra isn't installed, the automatic
daily push won't fire (same caveat as the hourly rollover check — see the
startup log warning), but `/morning` still works on demand regardless.

## How the evening nudge works

Every day at a fixed local time (`EVENING_NUDGE_HOUR`/`EVENING_NUDGE_MINUTE`
in `BOT_TIMEZONE`, default 8:30pm), Morrow checks whether *literally
nothing* has been logged yet today — no meal, no workout, no vitals
check-in (reusing the exact same real counts `/daystats` trusts, see "How
day stats works" above) — and if so, sends one gentle "quiet day so far"
message.

This is deliberately conservative: it only fires on a genuinely empty day,
never for a partial one (a workout logged but no meals yet is completely
normal and not worth interrupting for). The point is catching the one case
that's actually worth a nudge — a day nothing got logged at all — not
nagging about any specific domain. No AI call decides *whether* or *what*
to send (same reasoning as the morning briefing) — but the message text
does go through Morrow's companion voice before sending, same as every
other reply (see "How Morrow's companion voice works on every reply"
above), and the same per-chat try/except discipline applies, so one chat's
failure never blocks the nudge reaching anyone else.

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
- **Move one to a new day by natural language too** — "correct that to the
  23rd", "push day should be tomorrow, not today", "move dinner with Mel to
  next Tuesday" all reschedule the event in place, the same before/after
  confirmation and one-word `undo` as a delete. `/rescheduleevent <id>
  <days from today>` still works as the direct command-line equivalent.
  This is deliberately a *different* action from clearing one — "the event
  is moving to a different day" and "the event is going away entirely" are
  different requests, and confusing the two used to be a real, actively
  harmful bug: before natural-language rescheduling existed, a
  date-correction message had no matching action at all (only clearing an
  event did), and it got misread as a delete — silently destroying two real
  scheduled events instead of moving them. Both actions now exist
  end-to-end, so there's no ambiguous case left to guess wrong on.
- **Deliberately flat, not truly recurring.** There's no "every Wednesday"
  rule here — a real recurring event needs day-of-week/interval matching plus
  the classic "edit one occurrence vs. all future occurrences" problem,
  neither of which this codebase has an established pattern for yet. The
  actual near-term driver (a week's workout plan) is really just several
  flat dated rows regenerated by a fresh planning conversation each week, not
  a true recurring rule, so this covers it without guessing at a recurrence
  design ahead of a real driving case.

## How Telegram formatting works

Every outgoing message is sent with Telegram's HTML `parse_mode` (not
MarkdownV2 -- MarkdownV2 requires escaping punctuation like `-`, `.`, `!`,
`#` that shows up constantly in this bot's own text, like dates and
calorie ranges, so almost every existing string would have broken).
This is wired up in exactly one place: `app.py`'s `_FormattingBot`, a thin
`Bot` subclass that every `reply_text`/`send_message` call in the codebase
already routes through, which runs the outgoing text through
`tg_html.to_telegram_html` before it reaches Telegram. No domain module
needs to remember to escape anything or set a parse mode itself.

`to_telegram_html` first HTML-escapes the text (so a stray `&`, `<`, or
`>` in a logged description or note can never break Telegram's parser or
be mistaken for a tag), then promotes a small, deliberate set of
lightweight-markdown conventions into the matching Telegram tags:
`**bold**`, `` `inline code` ``, and a ` ```fenced block``` ` for
monospace column alignment. Claude's own freeform narrative prompts
(`casual_reply`, `/summary`, `/rundown`) are told to use these -- plus
plain unicode arrows (↑/↓/→) for a trend direction -- sparingly, and only
when a genuine comparison (e.g. "how did this week's workouts compare to
last week") would otherwise lose its shape as plain prose. Telegram has
no real table support in any client, so a literal markdown table is never
used -- it would just render as a wall of pipes and dashes; a short fenced
block is the substitute when something truly needs aligned columns.

`to_telegram_html` also normalizes a literal backslash-n (or
backslash-r-backslash-n) into a real line break before anything else runs.
This fixes a real observed bug: the rolling conversation history fed into
narration prompts is JSON-serialized, which turns a real newline inside an
earlier reply into that same two-character escape sequence within the JSON
string -- when the model closely echoes or paraphrases a near-identical
earlier reply, it could reproduce that raw serialized form verbatim instead
of a real newline, so the user would see literal `\n` characters in the
message. There's a matching prompt instruction too, but the normalization
here is the actual guarantee, the same defense-in-depth pattern used
elsewhere in this codebase (a prompt instruction alone already failed once
for the event-reschedule bug).

## Files

| File | Purpose |
|---|---|
| `bot.py` | Thin facade re-exporting every domain module's names (see its own docstring) |
| `app.py` | Entrypoint: builds the bot, registers every handler, the periodic rollover job |
| `tg_html.py` | Escapes + promotes lightweight markdown to Telegram HTML for every outgoing message (see "How Telegram formatting works") |
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
/daystats [today|yesterday|N]    real calories in/out + activity + vitals for ONE specific day
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
