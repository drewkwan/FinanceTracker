"""
Tests for replies._reply's companion-voice narration (ai.narrate_reply) --
the mechanism behind Andrew's "Morrow should feel like my personal
assistant/companion at all times, just like those ChatGPT threads" request,
not just during the 'casual' intent. See replies.py's module docstring and
ai.narrate_reply's docstring for the full story.

Every other test file in this suite relies on tests/conftest.py's
autouse `_passthrough_narration` fixture (ai.narrate_reply mocked to a pure
identity passthrough) so the large existing body of exact-confirmation-text
assertions keeps working without each one having to care that narration
exists. THIS file is where the narration mechanism itself is actually
exercised, by overriding that fixture's monkeypatch within each test body
(monkeypatch stacking: the later setattr wins, and both are cleanly undone
at teardown regardless of test outcome).
"""

import asyncio

import ai
import db
from conftest import CHAT
from test_meals_workouts import FakeUpdate
from test_morning import _FakeTickContext
from replies import _reply, _send_proactive


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_reply_narrates_by_default(monkeypatch):
    db.get_or_create_user(CHAT)
    update = FakeUpdate(CHAT)
    monkeypatch.setattr(ai, "narrate_reply", lambda text, *a, **kw: f"(companion voice) {text}")
    _run(_reply(update, CHAT, "Logged: pull-ups 10x3"))
    assert update.message.replies == ["(companion voice) Logged: pull-ups 10x3"]


def test_reply_sends_the_narrated_text_not_the_original(monkeypatch):
    """The narrated text is what actually reaches the user, so it's also
    what must be persisted to conversation history -- otherwise future
    turns would see a version of what Morrow said that it never actually
    sent."""
    db.get_or_create_user(CHAT)
    update = FakeUpdate(CHAT)
    monkeypatch.setattr(ai, "narrate_reply",
                         lambda text, *a, **kw: "Nice, three sets of pull-ups -- solid session.")
    _run(_reply(update, CHAT, "Logged: pull-ups 10x3"))
    stored = db.get_recent_messages(CHAT, limit=5)
    assert stored[0]["content"] == "Nice, three sets of pull-ups -- solid session."


def test_reply_with_narrate_false_bypasses_narration_entirely(monkeypatch):
    db.get_or_create_user(CHAT)
    update = FakeUpdate(CHAT)
    called = []

    def fake_narrate_reply(text, *args, **kwargs):
        called.append(text)
        return "SHOULD NOT BE USED"

    monkeypatch.setattr(ai, "narrate_reply", fake_narrate_reply)
    _run(_reply(update, CHAT, "You're doing fine this week.", narrate=False))
    assert not called
    assert update.message.replies == ["You're doing fine this week."]


def test_reply_falls_back_to_original_text_if_narration_fails(monkeypatch):
    """Same 'never go silent, never say something false' discipline as
    every other narration call in this codebase -- a narration API hiccup
    must never turn into a lost or garbled reply."""
    db.get_or_create_user(CHAT)
    update = FakeUpdate(CHAT)

    def fake_narrate_reply(text, *args, **kwargs):
        raise ConnectionError("API blip")

    monkeypatch.setattr(ai, "narrate_reply", fake_narrate_reply)
    _run(_reply(update, CHAT, "Logged: pull-ups 10x3"))
    assert update.message.replies == ["Logged: pull-ups 10x3"]


def test_reply_passes_real_grounding_context_to_narrate_reply(monkeypatch):
    """narrate_reply must get the same kind of real context answer_casually
    does -- otherwise it can't actually sound like a continuation of the
    conversation, and (more importantly) has no real data to check itself
    against when deciding how much to say."""
    db.get_or_create_user(CHAT)
    db.add_message(CHAT, "user", "just finished pull day")
    db.add_lift(CHAT, "pull-ups", "Visa gym", [{"reps": 10, "load": None}] * 3)
    update = FakeUpdate(CHAT)
    captured = {}

    def fake_narrate_reply(text, recent_messages=None, memory_list=None, today_snapshot=None,
                            recent_lifts=None):
        captured["recent_messages"] = recent_messages
        captured["today_snapshot"] = today_snapshot
        captured["recent_lifts"] = recent_lifts
        return text

    monkeypatch.setattr(ai, "narrate_reply", fake_narrate_reply)
    _run(_reply(update, CHAT, "Logged: v-bar rows 35kg 8x3"))
    assert any(m["content"] == "just finished pull day" for m in captured["recent_messages"])
    assert "balance" in captured["today_snapshot"] and "today" in captured["today_snapshot"]
    assert captured["recent_lifts"][0]["exercise"] == "pull-ups"


# ---------- _send_proactive: same treatment for scheduled sends ----------
# The morning briefing (morning.py) and evening nudge (nudges.py) have no
# Update to reply to -- they're pushed by app.py's job_queue on a schedule,
# not in response to a message -- so they go through this sibling of _reply
# instead. Same narration-by-default, same fallback, same conversation-
# history logging; see replies._send_proactive's own docstring.

def test_send_proactive_narrates_by_default(monkeypatch):
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()
    monkeypatch.setattr(ai, "narrate_reply", lambda text, *a, **kw: f"(companion voice) {text}")
    _run(_send_proactive(context, CHAT, "Good morning! Here's where things stand:"))
    assert context.bot.sent == [(CHAT, "(companion voice) Good morning! Here's where things stand:")]


def test_send_proactive_logs_the_narrated_text_to_conversation_history(monkeypatch):
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()
    monkeypatch.setattr(ai, "narrate_reply", lambda text, *a, **kw: "Morning! Nothing urgent today.")
    _run(_send_proactive(context, CHAT, "Good morning! Here's where things stand:"))
    stored = db.get_recent_messages(CHAT, limit=5)
    assert stored[0]["content"] == "Morning! Nothing urgent today."


def test_send_proactive_with_narrate_false_bypasses_narration_entirely(monkeypatch):
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()
    called = []

    def fake_narrate_reply(text, *args, **kwargs):
        called.append(text)
        return "SHOULD NOT BE USED"

    monkeypatch.setattr(ai, "narrate_reply", fake_narrate_reply)
    _run(_send_proactive(context, CHAT, "Quiet day so far.", narrate=False))
    assert not called
    assert context.bot.sent == [(CHAT, "Quiet day so far.")]


def test_send_proactive_falls_back_to_original_text_if_narration_fails(monkeypatch):
    db.get_or_create_user(CHAT)
    context = _FakeTickContext()

    def fake_narrate_reply(text, *args, **kwargs):
        raise ConnectionError("API blip")

    monkeypatch.setattr(ai, "narrate_reply", fake_narrate_reply)
    _run(_send_proactive(context, CHAT, "Quiet day so far."))
    assert context.bot.sent == [(CHAT, "Quiet day so far.")]
