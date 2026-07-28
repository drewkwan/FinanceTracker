"""
Claude-powered parsing and categorization.

Entry points:
  - parse_message(text, recent_expenses): classifies a free-text chat message
    into exactly one intent -- log_expense, correction (edit/delete something
    already logged), casual chat, or a clarifying question -- and extracts
    the structured fields needed to act on it. Never invents a target
    expense id outside the recent_expenses list it's given, and never claims
    an action was taken; that's entirely bot.py's job, using real DB values.
  - categorize(description): given a known amount/description (e.g. from
    /log or /claim), returns just the category tag.
  - answer_with_data(question, context_rows): used for on-demand analytics,
    turns raw category totals into a short natural-language answer.
  - answer_with_trends(period, payload): the /summary trend narrative.

The model is instructed to always return strict JSON so the bot can parse it
reliably without brittle regex. All date arithmetic and all "did this
actually happen" confirmations are handled deterministically in Python --
the model's job is classification and extraction only, never computing
dates or reporting on state changes itself.
"""

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
    "/edit <id> <amount> [description] (fix a mislogged expense), /delete <id> (remove by ID)"
)

PARSE_SYSTEM_PROMPT = f"""You read a short chat message sent to a personal expense-tracking Telegram bot \
and classify it into exactly ONE intent, extracting the fields needed to act on it. Categories you may use: \
{CATEGORY_LIST}. The user's default/base currency is {config.BASE_CURRENCY}. Currencies you may recognize: \
{CURRENCY_LIST}. The bot's real commands, for when you need to point the user at one: {COMMAND_LIST}.

You will also be given a JSON list of the user's most recently logged expenses (most recent first), each \
with an "id". This is the ONLY set of expenses you may reference -- never invent or guess an id that isn't \
in that list.

Respond with ONLY a JSON object, no other text, matching this shape:
{{
  "intent": "log_expense" | "correction" | "show_balance" | "show_recent" | "casual" | "clarification",

  "amount": number or null (log_expense only),
  "currency": one of the currency list, or null if not mentioned (log_expense only),
  "description": string or null (log_expense only),
  "category": one of the category list, or null if unclear (log_expense only),
  "is_claimable": true/false/null (log_expense only),

  "target_expense_id": integer or null (correction only -- MUST be an "id" from the provided recent-expenses list),
  "correction_action": "edit_date" | "edit_currency" | "edit_amount" | "edit_description" | "edit_category" | "delete" or null (correction only),
  "days_ago": integer or null (correction + edit_date only -- 0 = today, 1 = yesterday, 2 = two days ago, etc.
    up to 14. Extract WHICH day the user means as a plain count of days back; never compute or output an
    actual calendar date yourself, that's done in code),
  "new_currency": one of the currency list or null (correction + edit_currency only),
  "new_amount": number or null (correction + edit_amount only),
  "new_description": string or null (correction + edit_description only),
  "new_category": one of the category list or null (correction + edit_category only),

  "clarification_question": string or null (clarification only -- short and friendly),
  "casual_reply": string or null (casual only -- short, warm, in-character reply)
}}

Deciding the intent:
- "log_expense": the message is reporting a NEW purchase to track (e.g. "spent 12 on lunch", "20 USD taxi").
- "correction": the message is about something ALREADY logged -- fixing the currency/amount/description/
  category/date of a past entry, or asking to delete a duplicate/mistake (e.g. "that was SGD not USD", "that
  was for yesterday", "that was 2 days ago", "you double logged my lunch", "delete that", "actually it was $50
  not $15", "that log from yesterday was wrong, tag it to the day before instead"). Identify the ONE matching
  expense in the provided recent-expenses list -- match on whatever the message gives you: amount,
  description, OR just a date/day reference alone (e.g. "yesterday's log", "the one from Monday") is enough
  on its own if exactly one recent expense has that expense_date, even with no amount or description
  mentioned. Set target_expense_id to its "id". If nothing in the list clearly matches, or more than one
  plausibly does (e.g. two expenses were both logged yesterday and the message doesn't say which), do NOT
  guess -- use "clarification" instead and ask the user to specify (you can suggest /recent to see IDs). If a
  single message describes MORE THAN ONE correction (e.g. "the $100 was from two days ago, also you double
  logged my lunch"), do NOT fall back to "casual" just because it's compound -- pick whichever one is
  clearest/most specific and resolve that one as a normal "correction"; the user will follow up separately
  about the other one if your reply doesn't cover it.
- "show_balance": the message is asking to see the current balance/target/streak right now (e.g. "show me my
  balance", "what's my balance", "how much do I have left today", "how am I doing today"). This is answered
  directly and immediately with real numbers -- it is NOT a "casual" reply pointing at the /balance command,
  because that command does exactly this and there's no reason to make the user type it separately. Only use
  this for TODAY's live balance; a request for a specific past day's balance (which isn't tracked historically)
  should be "casual", explaining that limitation.
- "show_recent": the message is asking to see recently logged expenses (e.g. "show me today's log", "what
  have I logged recently", "show me my expenses"). Same reasoning as show_balance -- answer directly rather
  than pointing at /recent.
- "casual": the message isn't about logging, correcting, or checking balance/recent expenses (e.g. "hi",
  "thanks", small talk, or a question about what you can do more generally). Write a short, warm
  "casual_reply" as the person's friendly finance assistant -- 1-2 sentences, no markdown. If they ask about
  a capability the bot has (a summary, undoing something), point them at the real command instead of saying
  you can't help -- never deny something on the real command list above. If they ask for something the bot
  genuinely can't do (e.g. a specific past day's balance -- /balance only ever reflects today), say that
  plainly and suggest the closest real alternative (e.g. /summary for a spending trend over a period) instead
  of inventing a capability that doesn't exist. NEVER, in a casual_reply, claim OR PROMISE that you performed,
  edited, deleted, logged, or will look into/fix/note anything -- not "I've removed it", not "I'll take care
  of that", not "noted, I'll fix it" -- casual_reply only talks and has no way to follow up later, so any
  phrasing implying action past, present, or future is misleading. Any actual data change must go through
  "log_expense" or "correction" instead, in the same turn -- never deferred to "casual" with a promise.
- "clarification": something important is missing or ambiguous to safely act on -- a log_expense with no
  amount, or a correction with an unclear target. Ask ONE short, specific question.

Rules for log_expense fields:
- A bare currency SYMBOL with no letters (e.g. "$", "£") is ambiguous on its own -- default it to the base
  currency ({config.BASE_CURRENCY}) rather than assuming USD, unless the message also spells out an actual
  currency name/code (e.g. "USD", "US dollars", "20 dollars US"). Only set "currency" at all when a currency
  is explicitly named or unambiguously symbolized (e.g. "20 USD", "€15", "50 baht" -> THB); otherwise leave it
  null so it defaults to base currency. Don't ask a clarifying question about currency.
- If there's no amount mentioned in what's clearly an attempt to log an expense, use "clarification" and ask
  for the amount.
- Only ask about category if it's genuinely ambiguous (e.g. "spent 50 at Target" could be groceries,
  shopping, or household). Obvious cases (e.g. "uber", "coffee", "netflix") should NOT need clarification.
- Only ask about is_claimable if the message gives no hint either way AND the amount is large enough that it
  plausibly could be a reimbursable/business expense (e.g. over 50 in base currency). Small everyday
  purchases default is_claimable to false without asking.
- Keep clarification_question short and conversational, and only ask about ONE thing at a time (prioritize:
  amount > claimable > category > correction target).
"""


def parse_message(text: str, recent_expenses: list | None = None) -> dict:
    """recent_expenses: list of {id, amount, currency, description, category,
    expense_date, is_claimable} dicts, most recent first -- typically the
    last ~8 for this chat. Never raises -- if the Claude call itself fails
    (auth, rate limit, network blip, etc.), falls back to a clarification
    response so the bot always replies to the user instead of going silent.
    """
    recent_expenses = recent_expenses or []
    user_content = (
        f"Recent expenses (most recent first, only reference an id from here):\n"
        f"{json.dumps(recent_expenses)}\n\n"
        f"Message: {text}"
    )
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=350,
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


def _clarify_fallback(message: str) -> dict:
    return {
        "intent": "clarification",
        "amount": None,
        "currency": None,
        "description": None,
        "category": None,
        "is_claimable": None,
        "target_expense_id": None,
        "correction_action": None,
        "days_ago": None,
        "new_currency": None,
        "new_amount": None,
        "new_description": None,
        "new_category": None,
        "clarification_question": message,
        "casual_reply": None,
    }


def _safe_json(raw: str) -> dict:
    # Strip accidental code fences if the model adds them.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _clarify_fallback(
            "Sorry, I didn't quite catch that — could you rephrase it, e.g. 'spent 12 on lunch'?"
        )
