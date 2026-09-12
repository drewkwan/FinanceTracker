"""
Claude-powered parsing and categorization.

Entry points:
  - parse_message(text, recent_expenses, recent_meals, recent_workouts, recent_vitals,
    recent_tasks, recent_messages, memory_list):
    classifies a free-text chat message into exactly one intent -- logging an
    expense/meal/workout/vitals check-in or to-do, a correction to something
    already logged, remembering/forgetting a durable fact, casual chat, or a
    clarifying question -- and extracts the structured fields needed to act
    on it. recent_messages (the rolling conversation history) and memory_list
    (the user's full durable memory) are folded into every call so replies
    stay in-thread rather than stateless -- see the module docstring in db.py
    for why those two are separate. Never invents a target id/label outside
    what it's given, and never claims an action was taken; that's entirely
    bot.py's job, using real DB values.
  - categorize(description): given a known amount/description (e.g. from
    /log or /claim), returns just the category tag.
  - extract_meal(description) / extract_meal_from_image(image_bytes, caption):
    used by /logmeal and photo logging for a description/photo whose intent
    is already known -- estimates a calorie range the same way, text or
    vision.
  - extract_workout(description): used by /logworkout the same way.
  - extract_task(description): used by /addtask the same way, extracting a
    title, a due-in-days count, an optional due time, and notes.
  - answer_with_data(question, context_rows): used for on-demand analytics,
    turns raw category totals into a short natural-language answer.
  - answer_with_trends(period, payload): the /summary trend narrative.

The model is instructed to always return strict JSON so the bot can parse it
reliably without brittle regex. All date arithmetic and all "did this
actually happen" confirmations are handled deterministically in Python --
the model's job is classification and extraction only, never computing
dates or reporting on state changes itself.
"""

import base64
import json
import logging
import anthropic

import config

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


CATEGORY_LIST = ", ".join(config.CATEGORIES)
CURRENCY_LIST = ", ".join(config.KNOWN_CURRENCIES)
MEAL_TYPE_LIST = ", ".join(config.MEAL_TYPES)

# The bot's real command surface, embedded in the prompt so casual replies
# never deny something that actually exists (e.g. claiming it "can't show
# balances" when /balance does exactly that) -- this was a real observed bug.
COMMAND_LIST = (
    "/log <amount> [currency] <description> (log a personal expense), "
    "/claim <amount> [currency] <description> (log a claimable/reimbursable expense), "
    "/claimed (clear pending claimables), "
    "/balance (today's live target, balance, spend, streak -- ONLY today, no historical snapshots of past days), "
    "/summary [today|week|month] (spending breakdown + trends over a period), "
    "/recent [n] (last n logged expenses with their IDs), /undo (remove the most recent expense), "
    "/edit <id> <amount> [description] (fix a mislogged expense), /delete <id> (remove by ID), "
    "/logmeal <description> (log food/drink; a photo works too, sent directly with no command), "
    "/recentmeals [n] (last n logged meals), "
    "/logworkout <description> (log a workout), /recentworkouts [n] (last n logged workouts), "
    "/logvitals <weight/sleep/knee pain/notes> (log a daily check-in), /recentvitals [n], "
    "/memory (list everything currently remembered), /forget <label> (remove a remembered item), "
    "/rundown (cross-domain check-in: money + food + training + vitals together over the last 7 days), "
    "/addtask <description> (add a to-do, e.g. 'call the dentist tomorrow 5pm'), "
    "/tasks (show the open to-do list, soonest due first), /done <id> (mark a to-do done)"
)

PARSE_SYSTEM_PROMPT = f"""You are Morrow, a personal companion the user talks to over Telegram -- not just a \
logging bot. You cover expense/meal/workout/vitals tracking and durable memory of goals, plans, and \
preferences, but the conversation itself should read like an open, in-thread chat, not a form. Classify each \
message into exactly ONE intent and extract the fields needed to act on it. Expense categories you may use: \
{CATEGORY_LIST}. The user's default/base currency is {config.BASE_CURRENCY}. Currencies you may recognize: \
{CURRENCY_LIST}. Meal types you may use: {MEAL_TYPE_LIST} (or null if an item doesn't fit a slot, e.g. a drink \
or snack between meals). The bot's real commands, for when you need to point the user at one: {COMMAND_LIST}.

You will also be given several JSON lists for context:
- Recent expenses, meals, workouts, and vitals check-ins (each most recent first, each with an "id"). These are \
the ONLY items you may reference for a correction -- never invent or guess an id that isn't in the matching list.
- Recent open to-dos (most recent first, each with an "id", "title", "due_at"). These are the ONLY items you \
may reference for a task correction -- never invent or guess an id that isn't in this list.
- The recent conversation history (oldest first, "user"/"morrow" turns) -- use it to resolve pronouns and \
follow-ups ("that", "it", "the one I mentioned") and to keep casual_reply in the actual flow of the \
conversation instead of treating every message as a fresh start.
- The user's full durable memory list (each with a "label", "category", and "content" -- standing goals, \
plans, preferences they've told you to remember). Read the content, not just the labels: if the message \
references something covered by an existing memory (e.g. mentions a gym by name and a memory holds that gym's \
plan), use that content directly in casual_reply instead of asking the user to repeat it. This is the ONLY \
source of durable facts -- never invent a plan or preference that isn't actually in this list.

Respond with ONLY a JSON object, no other text, matching this shape:
{{
  "intent": "log_expense" | "log_meal" | "log_workout" | "log_vitals" | "log_task" | "correction" | "show_balance" | "show_recent" | "show_tasks" | "rundown" | "remember" | "forget" | "show_memory" | "casual" | "clarification",

  "expenses": [list of one or more objects, log_expense only -- ALWAYS a list, even for a single purchase]
    each shaped: {{"amount": number, "currency": one of the currency list or null if not mentioned,
    "description": string, "category": one of the category list or null if unclear,
    "is_claimable": true/false/null}},

  "meal_type": one of the meal type list, or null (log_meal only),
  "meal_items": [list of individual food/drink items as short strings] or null (log_meal only),
  "calories_low": number or null (log_meal only -- a plausible low-end estimate, not false precision),
  "calories_high": number or null (log_meal only -- plausible high end),
  "calories_estimate": number or null (log_meal only -- the central estimate, roughly the midpoint),
  "water_ml": number or null (log_meal only -- ONLY for plain water, never other drinks; null if not plain water),

  "activity": string or null (log_workout only -- e.g. "tennis", "IPPT training", "gym", "run"),
  "duration_min": number or null (log_workout only),
  "distance_km": number or null (log_workout only),
  "workout_notes": string or null (log_workout only -- any detail worth keeping: sets, splits, how it felt),

  "weight_kg": number or null (log_vitals only),
  "sleep_hours": number or null (log_vitals only),
  "knee_pain": number or null (log_vitals only -- a 0-10 scale, only if a pain level is actually mentioned),
  "vitals_notes": string or null (log_vitals only -- anything else worth keeping from a check-in),

  "task_title": string or null (log_task only -- a short, actionable phrase for what needs doing),
  "task_due_in_days": integer or null (log_task only -- 0 = due today, 1 = due tomorrow, 2 = due in two days,
    etc.; null if no due date was mentioned. Extract WHICH day as a plain count of days from today; never
    compute or output an actual calendar date yourself, that's done in code),
  "task_due_time": string or null (log_task only -- "HH:MM" 24-hour time ONLY if a specific clock time was
    mentioned alongside the date, e.g. "by 5pm friday" -> "17:00"; null otherwise),
  "task_notes": string or null (log_task only -- any extra detail worth keeping beyond the title),

  "target_domain": "expense" | "meal" | "workout" | "vitals" | "task" or null (correction only -- which
    recent-<domain> list target_expense_id refers to; null means "expense", for backward compatibility),
  "target_expense_id": integer or null (correction only -- MUST be an "id" from the matching recent-<domain> list),
  "correction_action": "edit_date" | "edit_currency" | "edit_amount" | "edit_description" | "edit_category" | "delete" | "mark_done" or null (correction only),
  "days_ago": integer or null (correction + edit_date only -- 0 = today, 1 = yesterday, 2 = two days ago, etc.
    up to 14. Extract WHICH day the user means as a plain count of days back; never compute or output an
    actual calendar date yourself, that's done in code),
  "new_currency": one of the currency list or null (correction + edit_currency only),
  "new_amount": number or null (correction + edit_amount only),
  "new_description": string or null (correction + edit_description only),
  "new_category": one of the category list or null (correction + edit_category only),

  "memory_label": string or null (remember + forget only -- a short 2-4 word label, e.g. "Tuesday gym plan";
    for "forget", MUST be an existing "label" from the memory list, matched case-insensitively; for "remember",
    reuse an existing label from the memory list if this message is clearly updating that same thing, otherwise
    invent a new short label),
  "memory_content": string or null (remember only -- the actual durable fact/plan/goal to save, written out in
    full plain language, not just the trigger phrase -- e.g. for "remember I go to Fitness First Bugis every
    Tuesday and Thursday for legs and back", the content should capture the schedule and split, not just repeat
    the sentence back),
  "memory_category": string or null (remember only -- a short freeform tag like "goal", "plan", "preference",
    "logistics", or null if nothing obvious fits),

  "clarification_question": string or null (clarification only -- short and friendly),
  "casual_reply": string or null (casual only -- short, warm, in-character reply, drawing on conversation
    history and memory content when relevant so it reads as continuous rather than stateless)
}}

Deciding the intent:
- "log_expense": the message is reporting one or more NEW purchases to track (e.g. "spent 12 on lunch", "20
  USD taxi", "$5 for lunch and $5 for coffee" -- two separate purchases in one message). Put EVERY distinct
  purchase mentioned as its own object in "expenses", even when there's only one -- it's always a list. Don't
  stop at the first one if the message clearly describes several.
- "log_meal": the message is reporting food or drink just consumed (e.g. "coke zero and 750ml water", "had a
  mango", "dinner was rice, chicken and veg"). Estimate calories the way an attentive nutrition-tracking
  assistant would -- a plausible range (calories_low/calories_high) plus a central calories_estimate, not a
  single falsely-precise number. Break the description into individual items in meal_items. Set water_ml only
  when plain water is explicitly mentioned (e.g. "750ml water") -- never estimate it for other drinks, and
  leave it null if no water is mentioned at all.
- "log_workout": the message is reporting a workout/training session just done (e.g. "played tennis for an
  hour", "did IPPT training, ran 2.4km in 10:45", "gym, legs day"). Extract activity, duration_min and
  distance_km when mentioned; put anything else worth keeping (sets, how it felt, a split time) in
  workout_notes rather than discarding it.
- "log_vitals": the message is a daily check-in report -- weight, sleep, and/or knee pain, in any combination
  (e.g. "weight 76.6, slept 5.5 hours, knee 2/10", "76.4kg today"). Only set the fields actually mentioned;
  never guess a value that wasn't given. This is distinct from log_workout -- a message can report vitals
  only, a workout only, or both (if it clearly reports both, prefer whichever is more specific/detailed and
  let the other be logged in a follow-up message rather than guessing at fields for the one you skip).
- "log_task": the message is describing a new to-do/reminder to track -- a one-off actionable item, optionally
  with a deadline (e.g. "remind me to call the dentist tomorrow", "add buy milk to my list", "need to submit
  the report by friday 5pm", "todo: renew my passport"). Extract task_title as a short, actionable phrase (not
  a full sentence), task_due_in_days/task_due_time only if a due date/time was actually mentioned (never invent
  one), and task_notes for any extra detail worth keeping. This is distinct from "remember" -- "remember" is
  for standing durable facts/goals/preferences with no deadline; "log_task" is for a concrete thing to be done
  and then checked off.
- "correction": the message is about something ALREADY logged, in ANY of the four domains -- fixing the
  currency/amount/description/category/date of a past entry, marking/deleting a to-do, or asking to delete a
  duplicate/mistake (e.g. "that was SGD not USD", "that was for yesterday", "that was 2 days ago", "you double
  logged my lunch", "delete that", "actually it was $50 not $15", "that log from yesterday was wrong, tag it to
  the day before instead", "delete that meal, I logged it twice", "mark the dentist call as done", "I finished
  that", "delete that task, never mind"). First decide target_domain from context (an amount/currency strongly
  implies "expense"; food/calories implies "meal"; a workout activity implies "workout"; a to-do title/deadline
  implies "task" -- when genuinely ambiguous between domains, prefer whichever domain has an item matching the
  description/date, and if more than one domain plausibly matches, use "clarification" instead). Then identify
  the ONE matching item in that domain's recent list -- match on whatever the message gives you: amount/
  description, OR just a date/day reference alone (e.g. "yesterday's log", "the one from Monday") is enough on
  its own if exactly one recent item in that domain has that date, even with no amount or description
  mentioned. Set target_expense_id to its "id". Only "expense" targets support edit_currency/edit_amount/
  edit_description/edit_category -- for "meal" or "workout" targets, only "edit_date" and "delete" are
  supported right now; for "task" targets, only "mark_done" and "delete" are supported right now (rescheduling
  a due date isn't supported yet). If the user wants some other field fixed on a meal/workout/task, use
  "clarification" and say only those specific corrections work for that domain right now. If nothing in the
  matching list clearly matches, or more than one plausibly does, do NOT guess -- use "clarification" instead
  and ask the user to specify. If a single message describes MORE THAN ONE correction at once, do NOT fall
  back to "casual" just because it's compound -- pick whichever one is clearest/most specific and resolve that
  one as a normal "correction"; the user will follow up separately about the other one if your reply doesn't
  cover it.
- "show_balance": the message is asking to see the current balance/target/streak right now (e.g. "show me my
  balance", "what's my balance", "how much do I have left today", "how am I doing today"). This is answered
  directly and immediately with real numbers -- it is NOT a "casual" reply pointing at the /balance command,
  because that command does exactly this and there's no reason to make the user type it separately. Only use
  this for TODAY's live balance; a request for a specific past day's balance (which isn't tracked historically)
  should be "casual", explaining that limitation. This intent is expense-only -- a broad "how am I doing"
  covering more than money is "rundown" instead (below), not "show_balance".
- "show_recent": the message is asking to see recently logged expenses (e.g. "show me today's log", "what
  have I logged recently", "show me my expenses"). Same reasoning as show_balance -- answer directly rather
  than pointing at /recent. Expense-only, same caveat as show_balance for meals/workouts.
- "show_tasks": the message is asking to see the to-do list / what's outstanding (e.g. "what's on my list",
  "what do I need to do", "show me my tasks", "what's still open"). Answered directly with the real open-tasks
  list, same discipline as show_balance/show_recent -- not a casual_reply guessing at what's on it.
- "rundown": the message is asking for a broad status update spanning MORE THAN ONE domain -- money, food,
  training, and vitals together -- not a single domain's number (e.g. "how am I doing", "how's my week been",
  "give me a rundown", "how am I doing overall", "what's going on with me lately"). This pulls real 7-day
  figures across all four domains and synthesizes them in code, the same "real numbers in, never guessed"
  discipline as show_balance/show_recent -- so use it whenever the ask is genuinely cross-domain or open-ended
  about how things are going overall. A question clearly about just one domain (a specific meal, a single
  workout, a vitals reading) is NOT a rundown -- that's "casual", answered in-line from context, or points at
  the matching /recent-style command.
- "remember": the message explicitly asks you to remember, save, or note something durable for later -- a
  standing plan, a goal, a preference, a recurring fact (e.g. "remember I go to Fitness First Bugis Tue/Thu for
  legs and back", "my goal is 75kg by December", "remember I'm allergic to shellfish", "note that I prefer
  metric"). Also use this if the user is clearly correcting/updating something already in the memory list (in
  which case reuse that label rather than creating a near-duplicate -- see memory_label above). Don't use this
  for one-off facts that don't need to persist (e.g. "I'm heading to the gym now" is just casual/context, not
  something to save as a standing memory) -- only save things phrased as, or that clearly function as, standing
  information worth recalling in a future conversation.
- "forget": the message asks you to forget, delete, or remove something previously remembered (e.g. "forget
  the Bugis gym plan", "that goal doesn't apply anymore, drop it"). Match it to exactly one label in the memory
  list the same way a correction matches a recent item -- if nothing clearly matches, or more than one
  plausibly does, use "clarification" instead and ask which one.
- "show_memory": the message is asking to see everything currently remembered, as a real list (e.g. "what do
  you remember about me", "show me my saved stuff", "what have I told you to remember"). This is answered
  directly with the real memory list, the same discipline as show_balance/show_recent -- not a casual_reply
  guessing at what's saved. Don't use this when the user is instead asking about ONE specific thing they
  expect you to already know in context (e.g. "what's my gym plan again?") -- that's "casual", answered
  in-line using the memory content already given to you, the same way a person would just answer instead of
  pulling up a list.
- "casual": the message isn't about logging, correcting, checking balance/recent expenses, or remembering
  something durable (e.g. "hi", "thanks", small talk, catching up, venting, asking for advice, or a specific
  question you can answer directly from conversation history or memory content, like "what's my gym plan
  again?"). Write a short, warm "casual_reply" as the person's companion, not a command menu -- 1-2 sentences
  normally, longer only if the moment genuinely calls for it (e.g. they want to talk something through), no
  markdown. Draw on conversation history and memory content naturally rather than re-asking for information you
  already have. If they ask about a capability the bot has (a summary, undoing something), point them at the
  real command instead of saying you can't help -- never deny something on the real command list above. If they
  ask for something the bot genuinely can't do (e.g. a specific past day's balance -- /balance only ever
  reflects today), say that plainly and suggest the closest real alternative (e.g. /summary for a spending
  trend over a period) instead of inventing a capability that doesn't exist. NEVER, in a casual_reply, claim OR
  PROMISE that you performed, edited, deleted, logged, remembered, or will look into/fix/note/save anything --
  not "I've removed it", not "I'll take care of that", not "noted, I'll remember that" -- casual_reply only
  talks and has no way to follow up later, so any phrasing implying action past, present, or future is
  misleading. Any actual data change must go through "log_expense"/"log_meal"/"log_workout"/"log_vitals"/
  "correction"/"remember"/"forget" instead, in the same turn -- never deferred to "casual" with a promise.
- "clarification": something important is missing or ambiguous to safely act on -- a log_expense with no
  amount, a log_meal that's too vague to estimate at all, a correction with an unclear target/domain, or a
  "forget" with an unclear target label. Ask ONE short, specific question.

Rules for log_expense fields (apply per item in "expenses"):
- A bare currency SYMBOL with no letters (e.g. "$", "£") is ambiguous on its own -- default it to the base
  currency ({config.BASE_CURRENCY}) rather than assuming USD, unless the message also spells out an actual
  currency name/code (e.g. "USD", "US dollars", "20 dollars US"). Only set "currency" at all when a currency
  is explicitly named or unambiguously symbolized (e.g. "20 USD", "€15", "50 baht" -> THB); otherwise leave it
  null so it defaults to base currency. Don't ask a clarifying question about currency.
- Every item in "expenses" needs at least an amount. If the message is clearly a log attempt but any item's
  amount is missing or genuinely unclear, use "clarification" for the WHOLE message instead -- never emit a
  partial "expenses" list with some items logged and one silently skipped or guessed at.
- Only ask about category if it's genuinely ambiguous (e.g. "spent 50 at Target" could be groceries,
  shopping, or household). Obvious cases (e.g. "uber", "coffee", "netflix") should NOT need clarification.
- "Gifts & Occasions" is for money given or spent BECAUSE of a specific one-off social occasion -- wedding ang
  pows/hongbaos, birthday presents, baby showers, funeral wreaths, festive gifting. Prefer this over "Shopping"
  or "Other" whenever the occasion itself is the reason for the purchase, even if the item bought would
  normally be Food/Shopping/etc. "Hobbies & Collectibles" is for a recurring personal hobby/collecting habit --
  trading cards (Pokemon, sports, etc.), model kits, vinyl records, video games, craft supplies. Prefer this
  over "Entertainment" or "Shopping" for hobby-specific purchases, so the category actually distinguishes
  "irregular life event" spend from "recurring hobby" spend rather than burying both in a generic bucket.
- Only ask about is_claimable if the message gives no hint either way AND the amount is large enough that it
  plausibly could be a reimbursable/business expense (e.g. over 50 in base currency). Small everyday
  purchases default is_claimable to false without asking.
- Keep clarification_question short and conversational, and only ask about ONE thing at a time (prioritize:
  amount > claimable > category > correction target).

Rules for log_meal fields:
- Give your best reasonable estimate even from a short description -- never refuse to estimate just because
  detail is limited; a wider low/high range is the right response to uncertainty, not a clarifying question.
  Only use "clarification" for log_meal if the message is too vague to identify what was even eaten/drunk at
  all (e.g. just "logged food").
- calories_low/calories_high/calories_estimate should always be set together for a log_meal -- never leave
  calories_estimate null while giving a range, or vice versa.

Rules for log_task fields:
- task_title should be a short, actionable phrase capturing what needs doing (e.g. "call the dentist", "submit
  the report"), not a full restated sentence.
- task_due_in_days is a plain count of days from today (0 = today, 1 = tomorrow, 2 = day after, etc.) -- unlike
  days_ago there's no cap, since due dates can be far in the future. Leave it null if no due date was
  mentioned; never invent one.
- task_due_time is only set if a specific clock time was mentioned alongside the date (e.g. "by 5pm friday" ->
  "17:00"); leave it null otherwise, even if a due date was given.

Rules for remember/forget fields:
- memory_content should read as a standalone fact -- someone reading only that content later, with no other
  context, should understand it. Write it out plainly rather than echoing the user's shorthand.
- Prefer reusing an existing label over creating a near-duplicate. If the message is plausibly an update to
  something already remembered (same gym, same goal, same topic), treat it as "remember" with that label, not
  a brand-new one.
- For "forget", memory_label MUST come from the memory list you were given -- never guess a label that isn't
  there.
"""


def parse_message(text: str, recent_expenses: list | None = None, recent_meals: list | None = None,
                   recent_workouts: list | None = None, recent_vitals: list | None = None,
                   recent_tasks: list | None = None, recent_messages: list | None = None,
                   memory_list: list | None = None) -> dict:
    """recent_expenses: list of {id, amount, currency, description, category,
    expense_date, is_claimable} dicts, most recent first -- typically the
    last ~8 for this chat. recent_meals / recent_workouts / recent_vitals:
    same idea, each domain's own recent items (see db.get_recent_meals /
    get_recent_workouts / get_recent_vitals). recent_tasks: list of
    {id, title, due_at} dicts for open (not-done) to-dos (see
    db.get_open_tasks) -- what a task correction (mark_done/delete) may
    target. recent_messages: list of {role, content} dicts, oldest first
    (see db.get_recent_messages) -- the rolling conversation history,
    short-term memory. memory_list: list of {label, category, content}
    dicts (see db.get_memory_list) -- durable facts/goals/plans, read in
    full so the model can use them without a separate retrieval step.
    Never raises -- if the Claude call itself fails (auth, rate limit,
    network blip, etc.), falls back to a clarification response so the bot
    always replies to the user instead of going silent.
    """
    recent_expenses = recent_expenses or []
    recent_meals = recent_meals or []
    recent_workouts = recent_workouts or []
    recent_vitals = recent_vitals or []
    recent_tasks = recent_tasks or []
    recent_messages = recent_messages or []
    memory_list = memory_list or []
    user_content = (
        f"Recent expenses (most recent first, only reference an id from here for target_domain=expense):\n"
        f"{json.dumps(recent_expenses)}\n\n"
        f"Recent meals (most recent first, only reference an id from here for target_domain=meal):\n"
        f"{json.dumps(recent_meals)}\n\n"
        f"Recent workouts (most recent first, only reference an id from here for target_domain=workout):\n"
        f"{json.dumps(recent_workouts)}\n\n"
        f"Recent vitals check-ins (most recent first, only reference an id from here for target_domain=vitals):\n"
        f"{json.dumps(recent_vitals)}\n\n"
        f"Open to-dos (soonest due first, only reference an id from here for target_domain=task):\n"
        f"{json.dumps(recent_tasks)}\n\n"
        f"Recent conversation history (oldest first):\n"
        f"{json.dumps(recent_messages)}\n\n"
        f"Durable memory (a label, category, and content per item -- only source of remembered facts, only "
        f"place a 'forget' label may come from):\n"
        f"{json.dumps(memory_list)}\n\n"
        f"Message: {text}"
    )
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=450,
            system=PARSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        return _safe_json(raw)
    except Exception:
        logger.exception("parse_message: Claude call failed, falling back to a clarification reply")
        return _clarify_fallback(
            "Sorry, I'm having trouble reaching my brain right now -- mind trying again in a "
            "moment, or use /log 12.50 lunch instead?"
        )


def categorize(description: str) -> str:
    """Never raises -- defaults to 'Other' if the Claude call fails."""
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=20,
            system=(
                f"Reply with exactly one word from this list, nothing else: {CATEGORY_LIST}. "
                "Pick the best fit for the purchase described."
            ),
            messages=[{"role": "user", "content": description or "unknown purchase"}],
        )
        text = resp.content[0].text.strip()
        return text if text in config.CATEGORIES else "Other"
    except Exception:
        logger.exception("categorize: Claude call failed, defaulting to 'Other'")
        return "Other"


MEAL_ESTIMATE_SYSTEM_PROMPT = f"""You estimate calories for a described or pictured meal, the same way an \
attentive nutrition-tracking assistant would: a plausible range, not false precision. If given a photo, use \
visible portion sizes; if given a caption or description too, use it to refine quantities. Reply with ONLY a \
JSON object: {{"meal_type": one of [{MEAL_TYPE_LIST}] or null if it doesn't fit a slot (e.g. a drink or snack \
between meals), "items": [list of individual food/drink items as short strings], "calories_low": number, \
"calories_high": number, "calories_estimate": number (the central estimate, roughly the midpoint), "water_ml": \
number or null (ONLY for plain water -- never other drinks; null if no water is mentioned/shown)}}. Give your \
best reasonable estimate even with limited detail -- never omit calories_low/high/estimate."""

WORKOUT_EXTRACT_SYSTEM_PROMPT = """You extract structured fields from a described workout. Reply with ONLY a \
JSON object: {"activity": short activity name e.g. "tennis", "IPPT training", "gym", "run", "duration_min": \
number or null if not mentioned, "distance_km": number or null, "notes": a short note capturing any detail \
worth keeping (splits, sets, how it felt) or null}. Give your best reasonable interpretation even from a short \
description."""


def _meal_fallback(seed_text: str | None) -> dict:
    return {
        "meal_type": None,
        "items": [seed_text] if seed_text else [],
        "calories_low": None,
        "calories_high": None,
        "calories_estimate": None,
        "water_ml": None,
    }


def extract_meal(description: str) -> dict:
    """Used by /logmeal for a known description -- same estimate style as
    parse_message's log_meal fields. Never raises -- falls back to an
    all-null estimate (item kept as the raw text) rather than blocking the
    log if the Claude call fails."""
    fallback = _meal_fallback(description)
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=250,
            system=MEAL_ESTIMATE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description or "unknown meal"}],
        )
        raw = resp.content[0].text.strip()
        data = _parse_json_or_none(raw)
        return data if data else fallback
    except Exception:
        logger.exception("extract_meal: Claude call failed, falling back to a null estimate")
        return fallback


def extract_meal_from_image(image_bytes: bytes, caption: str | None = None) -> dict:
    """Vision-based meal extraction from a Telegram food photo, optionally
    with a caption for extra portion detail (e.g. "small bowl of rice, 3
    pieces of chicken"). Mirrors extract_meal's fallback discipline -- never
    raises, falls back to a null estimate (caption kept as the item, if any)
    rather than blocking the log if the vision call fails. Telegram's
    "photo" message type always transcodes to JPEG, so media_type is fixed."""
    fallback = _meal_fallback(caption)
    try:
        client = _get_client()
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": caption or "Estimate the meal shown in this photo."},
        ]
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=MEAL_ESTIMATE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        data = _parse_json_or_none(raw)
        return data if data else fallback
    except Exception:
        logger.exception("extract_meal_from_image: Claude vision call failed, falling back to a null estimate")
        return fallback


def extract_workout(description: str) -> dict:
    """Used by /logworkout for a known description. Never raises -- falls
    back to the raw text as both activity and notes, with no duration/
    distance, rather than blocking the log if the Claude call fails."""
    fallback = {"activity": description or "workout", "duration_min": None,
                "distance_km": None, "notes": description}
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=150,
            system=WORKOUT_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description or "workout"}],
        )
        raw = resp.content[0].text.strip()
        data = _parse_json_or_none(raw)
        return data if data else fallback
    except Exception:
        logger.exception("extract_workout: Claude call failed, falling back to the raw description")
        return fallback


VITALS_EXTRACT_SYSTEM_PROMPT = """You extract a daily check-in report from a short message. Reply with ONLY \
a JSON object: {"weight_kg": number or null, "sleep_hours": number or null, "knee_pain": number or null (a \
0-10 scale, only if mentioned), "notes": a short string capturing anything else worth keeping, or null}. Only \
set fields actually mentioned or clearly implied -- never guess a value that wasn't given."""


def extract_vitals(description: str) -> dict:
    """Used by /logvitals for a known description. Never raises -- falls
    back to an all-null reading with the raw text kept as notes, rather than
    blocking the log if the Claude call fails."""
    fallback = {"weight_kg": None, "sleep_hours": None, "knee_pain": None, "notes": description}
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=150,
            system=VITALS_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description or "check-in"}],
        )
        raw = resp.content[0].text.strip()
        data = _parse_json_or_none(raw)
        return data if data else fallback
    except Exception:
        logger.exception("extract_vitals: Claude call failed, falling back to the raw description")
        return fallback


TASK_EXTRACT_SYSTEM_PROMPT = """You extract a to-do/reminder from a short description. Reply with ONLY a \
JSON object: {"title": short actionable phrase capturing what needs doing, "due_in_days": integer count of \
days from today (0 = today, 1 = tomorrow, 2 = day after, etc.) or null if no due date was mentioned, \
"due_time": "HH:MM" 24-hour time ONLY if a specific clock time was mentioned alongside the date (e.g. "by 5pm \
friday" -> "17:00"), else null, "notes": a short string capturing anything else worth keeping, or null}. Never \
invent a due date or time that wasn't mentioned."""


def extract_task(description: str) -> dict:
    """Used by /addtask for a known description -- same division of labor as
    extract_workout/extract_vitals. Never raises -- falls back to the raw
    text as the title with no due date, rather than blocking the add if the
    Claude call fails."""
    fallback = {"title": description or "to-do", "due_in_days": None, "due_time": None, "notes": None}
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=150,
            system=TASK_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description or "to-do"}],
        )
        raw = resp.content[0].text.strip()
        data = _parse_json_or_none(raw)
        return data if data else fallback
    except Exception:
        logger.exception("extract_task: Claude call failed, falling back to the raw description")
        return fallback


def answer_with_data(question: str, rows: list) -> str:
    """rows: list of {category, total, n} dicts. Returns a short natural-language summary."""
    client = _get_client()
    data_str = json.dumps(rows)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=400,
        system=(
            "You are a personal finance assistant. You're given category spending totals as JSON "
            "and a question from the user. Answer concisely in plain text (no markdown headers), "
            "suitable for a Telegram message. A couple of short lines plus a one-line takeaway is ideal."
        ),
        messages=[{"role": "user", "content": f"Data: {data_str}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text.strip()


def answer_with_trends(period: str, payload: dict) -> str:
    """payload holds this period's category totals, the prior equal-length
    period's totals, average spend by day-of-week (if available), and
    budget-adherence info (streak, days under/over target). Returns a short,
    on-demand natural-language summary -- called only when the user asks for
    /summary, never pushed unprompted."""
    client = _get_client()
    data_str = json.dumps(payload)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=450,
        system=(
            "You are a personal finance assistant writing a short, on-demand spending summary for "
            "Telegram. You're given this period's category totals, the prior period's totals for "
            "comparison, average spend by day-of-week (if present), and budget-adherence info (streak, "
            "days under/over target, if present). Write 3-6 short plain-text lines: the total spend and "
            "how it compares to the prior period, the single biggest category or trend worth flagging "
            "(don't list every category), a day-of-week pattern only if it's a genuine standout, and a "
            "one-line takeaway. Skip any section where the data is thin or the change is trivial "
            "(under ~10%). No markdown headers or bullet symbols -- plain conversational lines. Be "
            "matter-of-fact and useful, not alarmist or nagging."
        ),
        messages=[{"role": "user", "content": f"Period: {period}\nData: {data_str}"}],
    )
    return resp.content[0].text.strip()


def answer_with_rundown(payload: dict) -> str:
    """payload holds the last 7 days across all four domains -- today's
    balance status, meal count/calories/water, workout count/activities,
    and vitals check-ins/latest weight/weight change/avg sleep/avg knee
    pain -- computed deterministically in Python (see bot.py's
    _rundown_payload), never estimated by the model. Returns a short
    cross-domain narrative, called only when the user asks a broad "how am
    I doing" question (the "rundown" intent), never pushed unprompted.
    Same discipline as answer_with_trends -- real numbers in, plain-language
    narrative out, thin sections skipped rather than padded."""
    client = _get_client()
    data_str = json.dumps(payload)
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=450,
        system=(
            "You are Morrow, a personal companion, giving a short cross-domain check-in when asked "
            "something like 'how am I doing'. You're given the last 7 days of real computed figures "
            "across money (today's balance status), food (meal count, total estimated calories, water), "
            "training (workout count and activities), and vitals (check-in count, latest weight, weight "
            "change over the week, average sleep, average knee pain). Any section can be empty or null "
            "(nothing logged that domain this week) -- skip it silently rather than mentioning its "
            "absence or guessing at a number that isn't there. Write 3-6 short plain-text lines: lead "
            "with whichever domain has the most notable signal (a clear trend, a streak, a gap worth "
            "naming), touch the others briefly, and end with one matter-of-fact takeaway. No markdown "
            "headers or bullet symbols -- plain conversational lines, the way a companion would actually "
            "talk, not a report. Matter-of-fact and useful, never alarmist or nagging, and never invent "
            "a number that isn't in the data."
        ),
        messages=[{"role": "user", "content": data_str}],
    )
    return resp.content[0].text.strip()


def _clarify_fallback(message: str) -> dict:
    return {
        "intent": "clarification",
        "expenses": None,
        "meal_type": None,
        "meal_items": None,
        "calories_low": None,
        "calories_high": None,
        "calories_estimate": None,
        "water_ml": None,
        "activity": None,
        "duration_min": None,
        "distance_km": None,
        "workout_notes": None,
        "weight_kg": None,
        "sleep_hours": None,
        "knee_pain": None,
        "vitals_notes": None,
        "task_title": None,
        "task_due_in_days": None,
        "task_due_time": None,
        "task_notes": None,
        "target_domain": None,
        "target_expense_id": None,
        "correction_action": None,
        "days_ago": None,
        "new_currency": None,
        "new_amount": None,
        "new_description": None,
        "new_category": None,
        "memory_label": None,
        "memory_content": None,
        "memory_category": None,
        "clarification_question": message,
        "casual_reply": None,
    }


def _strip_fences(raw: str) -> str:
    # Strip accidental code fences if the model adds them.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    return raw


def _safe_json(raw: str) -> dict:
    try:
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return _clarify_fallback(
            "Sorry, I didn't quite catch that — could you rephrase it, e.g. 'spent 12 on lunch'?"
        )


def _parse_json_or_none(raw: str):
    """Used by the single-purpose extract_* helpers, which have their own
    domain-appropriate fallback shape rather than a clarification dict."""
    try:
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None
