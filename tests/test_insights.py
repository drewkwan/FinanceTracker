"""
Tests for the /summary "advisor-style insights" upgrade: a real historical
baseline and ratio per category (category_insights), and the actual largest
individual transactions in the period (top_transactions) -- both computed in
Python and handed to ai.answer_with_trends, never left for the model to
estimate. See summary.py's module comments and ai.py's TRENDS_SYSTEM_PROMPT
for the reasoning this is meant to support (a one-off occasion vs. a genuine
behavioral spike, and a category total vs. the one purchase actually driving
it).

ai.answer_with_trends is always mocked here (no real Claude calls) -- these
tests are about the payload Python builds, not the narrative Claude writes
from it.
"""

import asyncio
from datetime import date, timedelta

import bot
import db
from conftest import CHAT


class FakeMessage:
    def __init__(self, args_text=""):
        self.text = args_text
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id):
        self.message = FakeMessage()
        self.effective_chat = FakeChat(chat_id)


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _insert(chat_id, day, amount, category="Food", description="x", is_claimable=0):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (chat_id, amount, currency, amount_base, description, category, "
            "is_claimable, expense_date) VALUES (?, ?, 'SGD', ?, ?, ?, ?, ?)",
            (chat_id, amount, amount, description, category, is_claimable, day.isoformat()),
        )


# ---------- db.get_largest_expenses ----------

def test_get_largest_expenses_orders_desc_and_respects_limit():
    db.get_or_create_user(CHAT)
    today = date.today()
    for amount in (5, 50, 500, 15, 150):
        _insert(CHAT, today, amount)

    top = db.get_largest_expenses(CHAT, today.isoformat(), (today + timedelta(days=1)).isoformat(), limit=3)

    assert [round(r["amount_base"]) for r in top] == [500, 150, 50]


def test_get_largest_expenses_excludes_claimables_and_out_of_range_rows():
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert(CHAT, today, 1000, is_claimable=1)  # claimable -- must not appear
    _insert(CHAT, today - timedelta(days=30), 900)  # outside the queried range
    _insert(CHAT, today, 20)

    top = db.get_largest_expenses(CHAT, today.isoformat(), (today + timedelta(days=1)).isoformat(), limit=5)

    assert len(top) == 1
    assert top[0]["amount_base"] == 20


# ---------- summary()'s category_insights / top_transactions payload ----------

def _capture_payload(monkeypatch):
    captured = {}

    def fake_answer_with_trends(period, payload):
        captured["period"] = period
        captured["payload"] = payload
        return "a narrated summary"

    monkeypatch.setattr(bot.ai, "answer_with_trends", fake_answer_with_trends)
    return captured


def test_category_insights_ratio_reflects_real_baseline(monkeypatch):
    db.get_or_create_user(CHAT)
    today = date.today()
    captured = _capture_payload(monkeypatch)

    # Baseline window (6 weeks immediately before this week): 300 total for
    # Hobbies & Collectibles across those 6 weeks -> typical_per_period = 50.
    baseline_start = today - timedelta(days=7 * 6 + 6)  # comfortably inside the 42-day lookback
    _insert(CHAT, baseline_start, 300, category="Hobbies & Collectibles")
    # This week: a real 10x spike against that baseline.
    _insert(CHAT, today, 500, category="Hobbies & Collectibles")

    update = FakeUpdate(CHAT)
    _run(bot.summary(update, FakeContext(args=["week"])))

    insights = {i["category"]: i for i in captured["payload"]["category_insights"]}
    hobbies = insights["Hobbies & Collectibles"]
    assert hobbies["total"] == 500
    assert hobbies["typical_per_period"] == 50.0
    assert hobbies["vs_typical_ratio"] == 10.0


def test_category_with_no_baseline_history_is_null_not_zero(monkeypatch):
    db.get_or_create_user(CHAT)
    today = date.today()
    captured = _capture_payload(monkeypatch)

    # Nothing at all in the baseline window -- this category is brand new.
    _insert(CHAT, today, 80, category="Travel")

    update = FakeUpdate(CHAT)
    _run(bot.summary(update, FakeContext(args=["week"])))

    travel = next(i for i in captured["payload"]["category_insights"] if i["category"] == "Travel")
    assert travel["typical_per_period"] is None
    assert travel["vs_typical_ratio"] is None


def test_top_transactions_surfaces_the_actual_big_ticket_item(monkeypatch):
    db.get_or_create_user(CHAT)
    today = date.today()
    captured = _capture_payload(monkeypatch)

    _insert(CHAT, today, 10, category="Food", description="lunch")
    _insert(CHAT, today, 3000, category="Hobbies & Collectibles", description="pokemon packs")

    update = FakeUpdate(CHAT)
    _run(bot.summary(update, FakeContext(args=["week"])))

    top = captured["payload"]["top_transactions"]
    assert top[0]["description"] == "pokemon packs"
    assert top[0]["amount"] == 3000
    assert top[0]["category"] == "Hobbies & Collectibles"


def test_summary_still_falls_back_to_raw_breakdown_when_ai_call_fails(monkeypatch):
    """The new payload fields must not break the existing fallback path --
    it never reads category_insights/top_transactions, only the raw
    current_totals, matching /rundown's identical fallback discipline."""
    db.get_or_create_user(CHAT)
    today = date.today()
    _insert(CHAT, today, 42, category="Food")

    def _boom(period, payload):
        raise RuntimeError("simulated AI failure")

    monkeypatch.setattr(bot.ai, "answer_with_trends", _boom)

    update = FakeUpdate(CHAT)
    _run(bot.summary(update, FakeContext(args=["week"])))

    reply = update.message.replies[0]
    assert "Food" in reply
    assert "42" in reply
