"""
Tests for bot.py's conversational state handling in handle_text -- most
importantly the PENDING_KEY multi-round clarification accumulation, which
had a real production bug: each additional round of clarification replaced
the saved context with only the latest raw fragment instead of the
accumulated merged text, silently losing earlier information (e.g. an
amount mentioned two messages back) by the third round of back-and-forth.

ai.parse_message is always mocked here (no real Claude calls) via a queue of
canned responses. We only need duck-typed stand-ins for telegram's
Update/Context -- attribute access (message.reply_text, effective_chat.id,
chat_data) is all handle_text touches, not any real python-telegram-bot
machinery.
"""

import asyncio

import bot
import db
from conftest import CHAT


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id, text):
        self.message = FakeMessage(text)
        self.effective_chat = FakeChat(chat_id)


class FakeContext:
    def __init__(self):
        self.chat_data = {}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _clarification(question):
    return {
        "intent": "clarification", "clarification_question": question,
        "amount": None, "currency": None, "description": None, "category": None, "is_claimable": None,
        "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        "casual_reply": None,
    }


def _log_expense(amount, description, category):
    return {
        "intent": "log_expense", "amount": amount, "currency": None, "description": description,
        "category": category, "is_claimable": False,
        "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        "clarification_question": None, "casual_reply": None,
    }


def test_multi_round_clarification_accumulates_context_instead_of_overwriting(monkeypatch):
    """Regression test for the real bug: round 1 mentions the amount, round 2
    mentions the description, and only round 3 finally resolves -- if context
    were being overwritten each round (the actual bug), round 3 would only
    see round 2's fragment and the amount from round 1 would be lost."""
    db.get_or_create_user(CHAT)
    context = FakeContext()

    responses = iter([
        _clarification("What was that for?"),
        _clarification("How much did you spend?"),
        _log_expense(100.0, "parking cashcard top-up", "Transport"),
    ])
    captured_texts = []

    def fake_parse_message(text, recent_expenses=None):
        captured_texts.append(text)
        return next(responses)

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)

    update1 = FakeUpdate(CHAT, "I paid on my cash card 2 days ago")
    _run(bot.handle_text(update1, context))
    assert context.chat_data[bot.PENDING_KEY]["original"] == "I paid on my cash card 2 days ago"

    update2 = FakeUpdate(CHAT, "top up my parking cashcard")
    _run(bot.handle_text(update2, context))
    saved = context.chat_data[bot.PENDING_KEY]["original"]
    assert "cash card" in saved, "round 1's info must survive into round 2's saved context"
    assert "top up my parking cashcard" in saved, "round 2's info must also be present"

    update3 = FakeUpdate(CHAT, "$100")
    _run(bot.handle_text(update3, context))
    final_text_sent = captured_texts[-1]
    assert "cash card" in final_text_sent, "amount context from round 1 must still reach the final parse"
    assert "top up my parking cashcard" in final_text_sent
    assert "$100" in final_text_sent
    # two replies are expected here: the log confirmation, then a budget-alert
    # follow-up (100/100 default target crosses the alert threshold) -- check
    # any reply, not just the last one, for the actual log confirmation.
    assert any("Logged:" in r for r in update3.message.replies)
    assert bot.PENDING_KEY not in context.chat_data  # cleared once resolved


def _no_op_extra_fields():
    return {
        "amount": None, "currency": None, "description": None, "category": None, "is_claimable": None,
        "target_expense_id": None, "correction_action": None, "days_ago": None,
        "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
    }


def test_show_balance_answers_directly_with_real_numbers(monkeypatch):
    """'show me my balance' must answer immediately with the real numbers --
    the same output /balance would give -- not a casual reply pointing the
    user at the /balance command they already know exists."""
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    db.add_expense(CHAT, 20, "SGD", "coffee", "Food")
    context = FakeContext()

    def fake_parse_message(text, recent_expenses=None):
        return {"intent": "show_balance", "clarification_question": None, "casual_reply": None,
                **_no_op_extra_fields()}

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, "show me today's balance")
    _run(bot.handle_text(update, context))
    assert update.message.replies[-1] == bot._balance_text(CHAT)
    assert "Spent today" in update.message.replies[-1]


def test_show_recent_answers_directly_with_real_data(monkeypatch):
    db.get_or_create_user(CHAT)
    db.add_expense(CHAT, 15, "SGD", "cab ride", "Transport")
    context = FakeContext()

    def fake_parse_message(text, recent_expenses=None):
        return {"intent": "show_recent", "clarification_question": None, "casual_reply": None,
                **_no_op_extra_fields()}

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, "show me my recent expenses")
    _run(bot.handle_text(update, context))
    assert update.message.replies[-1] == bot._recent_text(CHAT)
    assert "cab ride" in update.message.replies[-1]


def test_correction_can_target_by_date_reference_alone(monkeypatch):
    """Regression test for a real user complaint: 'that log from yesterday
    was wrong, tag it to the day before instead' gives no amount or
    description at all -- only a date reference. The correction must still
    resolve, using expense_date matching rather than requiring amount/desc."""
    import datetime as dt
    db.get_or_create_user(CHAT)
    db.set_daily_target(CHAT, 100)
    yesterday = dt.date.today() - dt.timedelta(days=1)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, 50, 'SGD', 50, 'groceries', 'Groceries', 0, ?)",
            (CHAT, yesterday.isoformat()),
        )
    target = db.get_recent_expenses(CHAT, limit=1)[0]
    context = FakeContext()

    def fake_parse_message(text, recent_expenses=None):
        return {
            "intent": "correction", "target_expense_id": target["id"], "correction_action": "edit_date",
            "days_ago": 2, "clarification_question": None, "casual_reply": None,
            "amount": None, "currency": None, "description": None, "category": None, "is_claimable": None,
            "new_currency": None, "new_amount": None, "new_description": None, "new_category": None,
        }

    monkeypatch.setattr(bot.ai, "parse_message", fake_parse_message)
    update = FakeUpdate(CHAT, "that log from yesterday was wrong, tag it to the day before instead")
    _run(bot.handle_text(update, context))
    updated = db.get_expense(CHAT, target["id"])
    assert updated["expense_date"] == (dt.date.today() - dt.timedelta(days=2)).isoformat()
    assert "now dated" in update.message.replies[-1]
