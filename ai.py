"""
Claude-powered parsing and categorization.

Two entry points:
  - parse_message(text): turns a free-text chat message like
    "spent 12.50 on lunch, claimable" into a structured expense, asking a
    clarifying question if something important is missing or ambiguous.
  - categorize(description): given a known amount/description (e.g. from
    /log or /claim), returns just the category tag.
  - answer_with_data(question, context_rows): used for on-demand analytics,
    turns raw category totals into a short natural-language answer.

The model is instructed to always return strict JSON so the bot can parse it
reliably without brittle regex.
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

PARSE_SYSTEM_PROMPT = f"""You extract structured expense data from a short chat message someone sends \
to their personal expense-tracking bot. Categories you may use: {CATEGORY_LIST}. \
The user's default/base currency is {config.BASE_CURRENCY}. Currencies you may recognize: {CURRENCY_LIST}.

Respond with ONLY a JSON object, no other text, matching this shape:
{{
  "is_expense": true/false,
  "amount": number or null,
  "currency": one of the currency list, or null if not mentioned (assume base currency when null),
  "description": string or null,
  "category": one of the category list, or null if unclear,
  "is_claimable": true/false/null (null if the message doesn't say and it's not obvious),
  "needs_clarification": true/false,
  "clarification_question": string or null (a short, friendly question to ask the user, only if needs_clarification is true),
  "casual_reply": string or null (only set this when is_expense is false AND needs_clarification is false --
    a short, natural, in-character reply to send back instead of logging anything)
}}

Rules:
- If the message clearly isn't about logging an expense (e.g. "hi", "how's it going", "thanks", a random \
question, "/help"), set is_expense to false and needs_clarification to false, and write a short, warm, \
conversational "casual_reply" as if you're the person's friendly personal finance assistant chatting with \
them -- not a form. Feel free to be a little personable, but keep it brief (1-2 sentences, no markdown), and \
if it's natural, you can gently remind them what you're for (e.g. "also happy to log an expense whenever").
- If there's no amount mentioned in what's clearly an attempt to log an expense, set needs_clarification true \
and ask for the amount (leave casual_reply null in this case).
- Only set "currency" when the message explicitly names or symbolizes a different currency (e.g. "20 USD", \
"€15", "50 baht" -> THB). Don't ask a clarifying question about currency -- just default to null (base \
currency) if it's not clearly stated.
- Only ask about category if it's genuinely ambiguous (e.g. "spent 50 at Target" could be groceries, \
shopping, or household - ask). Obvious cases (e.g. "uber", "coffee", "netflix") should NOT need clarification.
- Only ask about is_claimable if the message gives no hint either way AND the amount is large enough that \
it plausibly could be a reimbursable/business expense (e.g. over 50 in base currency). Small everyday \
purchases should default is_claimable to false without asking.
- Keep clarification_question short and conversational, and only ask about ONE thing at a time \
(prioritize: amount > claimable > category).
"""


def parse_message(text: str) -> dict:
    """Never raises -- if the Claude call itself fails (auth, rate limit,
    network blip, etc.), falls back to a clarification response so the bot
    always replies to the user instead of going silent."""
    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=PARSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}],
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
        "is_expense": False,
        "amount": None,
        "currency": None,
        "description": None,
        "category": None,
        "is_claimable": None,
        "needs_clarification": True,
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
