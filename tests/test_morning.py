"""
Tests for the morning briefing: morning.py's deterministic payload/text
builders, /morning, and the automatic daily push (morning_briefing_tick).
Same discipline as the rest of the suite: no real network, no real Claude
calls (the briefing makes no AI call at all -- see morning.py's module
docstring for why), throwaway SQLite per test.
"""

import asyncio
import datetime as dt

import bot
import db
from conftest import CHAT
from test_meals_workouts import FakeContext, FakeUpdate


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def _days_ahead(n):
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


# ---------- morning.py: payload ----------

def test_payload_includes_only_tasks_due_today_or_overdue():
    db.get_or_create_user(CHAT)
    overdue_id = db.add_task(CHAT, "overdue task", due_at=_days_ago(2))
    today_id = db.add_task(CHAT, "due today", due_at=db.today_str())
    db.add_task(CHAT, "due later this week", due_at=_days_ahead(3))
    db.add_task(CHAT, "no due date at all")

    payload = bot._morning_briefing_payload(CHAT)
    due_ids = {t["id"] for t in payload["due_today_or_overdue"]}
    assert due_ids == {overdue_id, today_id}


def test_payload_excludes_done_tasks():
    db.get_or_create_user(CHAT)
    task_id = db.add_task(CHAT, "already done", due_at=_days_ago(1))
    db.mark_task_done(CHAT, task_id)
    payload = bot._morning_briefing_payload(CHAT)
    assert payload["due_today_or_overdue"] == []


def test_payload_yesterday_recap_excludes_today_and_older_days():
    db.get_or_create_user(CHAT)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, items, calories_estimate) VALUES (?, ?, '[]', 999)",
            (CHAT, _days_ago(2)),  # too old -- must not be counted
        )
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, items, calories_estimate) VALUES (?, ?, '[]', 500)",
            (CHAT, _days_ago(1)),  # yesterday -- must be counted
        )
        conn.execute(
            "INSERT INTO meals (chat_id, meal_date, items, calories_estimate) VALUES (?, ?, '[]', 300)",
            (CHAT, db.today_str()),  # today -- must not be counted (that's what the running total is for)
        )
    payload = bot._morning_briefing_payload(CHAT)
    assert payload["yesterday"]["calories"] == 500


def test_payload_yesterday_recap_is_null_when_nothing_was_logged():
    db.get_or_create_user(CHAT)
    payload = bot._morning_briefing_payload(CHAT)
    y = payload["yesterday"]
    assert y["calories"] is None
    assert y["workout_activities"] == []
    assert y["vitals_checkins"] == 0


# ---------- morning.py: text rendering ----------

def test_text_includes_balance_and_flags_overdue_tasks():
    db.get_or_create_user(CHAT)
    db.add_task(CHAT, "renew passport", due_at=_days_ago(3))
    db.add_task(CHAT, "call the dentist", due_at=db.today_str())
    payload = bot._morning_briefing_payload(CHAT)
    text = bot._morning_briefing_text(payload)
    assert "Today's target" in text
    assert "renew passport" in text and "[overdue]" in text
    assert "call the dentist" in text
    # the due-today task's own line must NOT be tagged overdue
    dentist_line = next(ln for ln in text.splitlines() if "call the dentist" in ln)
    assert "[overdue]" not in dentist_line


def test_text_omits_due_section_when_nothing_is_due():
    db.get_or_create_user(CHAT)
    payload = bot._morning_briefing_payload(CHAT)
    text = bot._morning_briefing_text(payload)
    assert "Due today or overdue" not in text


def test_text_includes_yesterdays_recap_when_present():
    db.get_or_create_user(CHAT)
    db.add_workout(CHAT, "run", distance_km=5)
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE workouts SET workout_date = ? WHERE chat_id = ?",
            (_days_ago(1), CHAT),
        )
    payload = bot._morning_briefing_payload(CHAT)
    text = bot._morning_briefing_text(payload)
    assert "Yesterday" in text and "run" in text


# ---------- /morning and the automatic daily push ----------

def test_morning_cmd_replies_with_the_briefing():
    db.get_or_create_user(CHAT)
    db.add_task(CHAT, "renew passport", due_at=db.today_str())
    update = FakeUpdate(CHAT, text="/morning")
    _run(bot.morning_cmd(update, FakeContext()))
    reply = update.message.replies[-1]
    assert "Good morning" in reply
    assert "renew passport" in reply


class _FakeBot:
    def __init__(self, fail_for_chat_id=None):
        self.sent = []
        self._fail_for_chat_id = fail_for_chat_id

    async def send_message(self, chat_id, text):
        if chat_id == self._fail_for_chat_id:
            raise RuntimeError("simulated send failure (e.g. user blocked the bot)")
        self.sent.append((chat_id, text))


class _FakeTickContext:
    def __init__(self, fail_for_chat_id=None):
        self.bot = _FakeBot(fail_for_chat_id)


def test_morning_briefing_tick_sends_to_every_known_chat():
    other_chat = CHAT + 1
    db.get_or_create_user(CHAT)
    db.get_or_create_user(other_chat)
    context = _FakeTickContext()
    _run(bot.morning_briefing_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert sent_chat_ids == {CHAT, other_chat}
    assert all("Good morning" in text for _, text in context.bot.sent)


def test_morning_briefing_tick_one_chats_failure_does_not_block_the_rest():
    """Mirrors app.rollover_tick's per-chat try/except -- one chat failing
    to receive the message (e.g. they blocked the bot) must not stop the
    briefing from reaching everyone else."""
    other_chat = CHAT + 1
    db.get_or_create_user(CHAT)
    db.get_or_create_user(other_chat)
    context = _FakeTickContext(fail_for_chat_id=CHAT)
    _run(bot.morning_briefing_tick(context))
    sent_chat_ids = {chat_id for chat_id, _ in context.bot.sent}
    assert sent_chat_ids == {other_chat}  # CHAT failed, other_chat still got it
