"""
Tests for tg_html.to_telegram_html (the Telegram-formatting-polish work --
see its own module docstring) and app._FormattingBot, the Bot subclass that
actually runs every outgoing send through it. No real Telegram/network
calls -- app._FormattingBot.send_message is exercised by monkeypatching its
parent class's send_message to a stub that just records what it was called
with, the same "swap the one real side effect for a spy" discipline used
throughout this test suite for ai._get_client.
"""

import asyncio

import pytest

import app
from tg_html import to_telegram_html


def test_plain_text_passes_through_unchanged():
    assert to_telegram_html("Logged: #5 chicken rice -- ~650 kcal (2026-09-17)") == (
        "Logged: #5 chicken rice -- ~650 kcal (2026-09-17)"
    )


def test_none_passes_through():
    assert to_telegram_html(None) is None


def test_ampersand_is_escaped_so_telegram_cant_reject_the_message():
    # The real bug this guards against: an unescaped "&" in user-entered text
    # (a description, a note) makes Telegram's HTML parser reject the whole
    # message as unparseable entities -- which reads to the user as no reply.
    assert to_telegram_html("mom & dad's AT&T bill") == "mom &amp; dad's AT&amp;T bill"


def test_raw_angle_brackets_are_escaped_not_treated_as_tags():
    # A stray "<" or ">" in logged text (or an attempted tag injection) must
    # never be passed through as if it were real markup.
    assert to_telegram_html("<3 you, and </br> too") == "&lt;3 you, and &lt;/br&gt; too"


def test_bold_markdown_is_promoted_to_html():
    assert to_telegram_html("This week **beat** last week") == "This week <b>beat</b> last week"


def test_inline_code_is_promoted_to_html():
    assert to_telegram_html("Set to `edit_meal` mode") == "Set to <code>edit_meal</code> mode"


def test_fenced_block_is_promoted_to_pre_for_monospace_alignment():
    text = "```\nMon  30min run\nTue  --\n```"
    assert to_telegram_html(text) == "<pre>Mon  30min run\nTue  --</pre>"


def test_fenced_block_with_language_hint_is_still_promoted():
    text = "```text\nMon  30min run\n```"
    assert to_telegram_html(text) == "<pre>Mon  30min run</pre>"


def test_unicode_trend_arrows_pass_through_untouched():
    assert to_telegram_html("Spend is up 12% this week ↑ vs last ↓") == (
        "Spend is up 12% this week ↑ vs last ↓"
    )


def test_escaping_happens_before_markdown_promotion_not_after():
    # A literal "&" living INSIDE an intentionally-bolded span must still be
    # escaped -- escaping first (which never touches * or `) guarantees this
    # regardless of order, per to_telegram_html's own docstring.
    assert to_telegram_html("**mom & dad**") == "<b>mom &amp; dad</b>"


# ---------- literal backslash-n normalization ----------
# Regression guards for a real observed bug: at least two of Morrow's own
# replies contained the two literal characters "\n" as visible text instead
# of an actual line break -- see the module docstring for the suspected
# mechanism (echoing JSON-serialized conversation history verbatim).

def test_literal_backslash_n_becomes_a_real_line_break():
    text = "Your push day at Visa:\\n\\nPull-ups: bodyweight 3x8"
    assert to_telegram_html(text) == "Your push day at Visa:\n\nPull-ups: bodyweight 3x8"


def test_literal_backslash_r_backslash_n_is_also_normalized():
    text = "line one\\r\\nline two"
    assert to_telegram_html(text) == "line one\nline two"


def test_real_newlines_are_left_untouched():
    # A real newline character (not the two-character escape sequence) must
    # pass straight through unchanged -- this fix is only for the literal
    # text form, not a general "reformat all line breaks" pass.
    text = "line one\nline two"
    assert to_telegram_html(text) == "line one\nline two"


def test_normalized_newline_still_interacts_correctly_with_fenced_blocks():
    # The fence regex expects real newlines after a language hint -- prove
    # normalization runs BEFORE fence promotion, not after, so a literal
    # backslash-n reaching Telegram inside a fenced block still becomes a
    # real line break rather than surviving as visible text.
    text = "```\\nMon  30min run\\nTue  --\\n```"
    assert to_telegram_html(text) == "<pre>Mon  30min run\nTue  --</pre>"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _RecordingParentSendMessage:
    """Stands in for ExtBot.send_message -- records exactly what it was
    called with so we can assert _FormattingBot transformed only `text`,
    nothing else, regardless of whether the caller passed it positionally
    or by keyword (real call sites in this codebase do both -- see
    _FormattingBot's own docstring)."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "sent"


@pytest.fixture
def recording_bot(monkeypatch):
    recorder = _RecordingParentSendMessage()
    monkeypatch.setattr(app.ExtBot, "send_message", recorder)
    bot = app._FormattingBot(token="123456:FAKE-TOKEN-FOR-TESTS")
    return bot, recorder


def test_formatting_bot_transforms_text_passed_as_keyword(recording_bot):
    bot, recorder = recording_bot
    _run(bot.send_message(chat_id=42, text="mom & dad's **plan**"))
    (args, kwargs) = recorder.calls[0]
    assert kwargs["text"] == "mom &amp; dad's <b>plan</b>"
    assert kwargs["chat_id"] == 42


def test_formatting_bot_transforms_text_passed_positionally(recording_bot):
    bot, recorder = recording_bot
    _run(bot.send_message(42, "mom & dad's **plan**"))
    (args, kwargs) = recorder.calls[0]
    assert args == (42, "mom &amp; dad's <b>plan</b>")


def test_formatting_bot_leaves_other_kwargs_untouched(recording_bot):
    bot, recorder = recording_bot
    _run(bot.send_message(chat_id=42, text="hi", disable_notification=True))
    (args, kwargs) = recorder.calls[0]
    assert kwargs["disable_notification"] is True
