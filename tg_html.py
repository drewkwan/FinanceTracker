"""
Turns whatever text Morrow is about to send into what Telegram's HTML
parse_mode actually expects. This is the one place any outgoing message
becomes safe to send as HTML -- see app.py's _FormattingBot, the single
chokepoint (a Bot subclass overriding send_message) that runs EVERY
outgoing message through this before it reaches Telegram's API, so no
domain module (finance.py, tasks.py, correction.py, ...) has to remember
to escape or set parse_mode itself, whether it sends via replies._reply,
a plain update.message.reply_text, or context.bot.send_message.

Why HTML mode at all: before this, no parse_mode was set anywhere, so
Telegram rendered every reply as literal plain text -- any markdown a
prompt asked Claude for (bold, structure) just showed up as literal
asterisks. Telegram's other option, MarkdownV2, requires escaping a long
list of punctuation (-, ., !, #, (, ), ...) that shows up constantly in
this bot's own text (dates like 2026-09-17, "#5", calorie ranges like
"~850-1050 kcal") -- switching every existing plain string to MarkdownV2
would have broken almost all of them. HTML mode only cares about three
characters (&, <, >), which barely appear in this bot's text at all, so
it's the only parse mode that's safe to turn on globally without rewriting
every hardcoded string in the codebase.

Telegram has no real table support in any client, so a structured
comparison ("this week vs last week") still can't be a literal markdown
table -- see ai.TELEGRAM_FORMATTING_NOTE for the small, deliberate set of
conventions Claude's own narrative prompts (casual_reply, /summary,
/rundown) are asked to use instead: **bold**, `inline code`, a ```fenced
block``` for monospace column alignment, and plain unicode arrows for a
trend. This module is what actually turns that syntax into the matching
Telegram HTML tags, safely, for freeform model text AND for Morrow's own
deterministic confirmation strings (which never use that syntax, and pass
through unchanged except for entity-escaping).

Also normalizes literal backslash-n: a real observed bug where at least
two of Morrow's own narrative replies contained the two literal characters
"\n" as visible text instead of an actual line break. The conversation
history fed into narration prompts (see ai.answer_casually) is JSON-
serialized, which turns a real newline inside a PRIOR turn's text into
that same two-character escape sequence within the JSON string -- when the
model closely echoes or paraphrases a near-identical earlier reply, it can
reproduce that raw serialized form verbatim instead of "decoding" it back
into a real newline. Fixed with a prompt instruction too (see
TELEGRAM_FORMATTING_NOTE), but a prompt instruction alone already failed
once for a structurally similar bug (events defaulting to "delete" -- see
events.py's docstring), so this is the same defense-in-depth: a normalize
step here catches it even if the model ignores or forgets the instruction.
"""

import html
import re

# Fenced blocks first, so a stray backtick or asterisk *inside* one (rare,
# but not impossible in a monospace comparison) isn't independently picked
# up by the narrower patterns below.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z]*\n)?(.*?)```", re.DOTALL)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
# Literal backslash-n (or backslash-r-backslash-n) as two/four visible
# characters, NOT an actual newline -- see the module docstring above for
# why this shows up at all. Deliberately global/unconditional: this bot's
# own text (deterministic confirmations, prompts) never has a legitimate
# reason to send a literal "\n" as content a user should read as-is, so
# there's no real case this could wrongly mangle.
_LITERAL_NEWLINE_RE = re.compile(r"\\r\\n|\\n")


def to_telegram_html(text: str | None) -> str | None:
    """Safe to call on ANY outgoing text. Escaping (html.escape, which only
    ever touches &, <, and >) always runs FIRST, before any of the markdown
    promoted below -- a raw "&" in a logged description or note (e.g. "AT&T
    bill") must never reach Telegram unescaped, since Telegram would then
    either drop it or reject the whole message as unparseable entities,
    which reads to the user as no reply at all. Because html.escape never
    touches *, `, or the ``` fence markers, escaping first and promoting
    second is always safe and order-independent for anything this bot
    actually sends -- plain text with none of these markers just comes back
    with &/</> escaped and is otherwise unchanged. The literal-backslash-n
    normalization runs first of all, before escaping -- it's fixing up a
    real newline that got flattened to text, not something that needs
    HTML-safety treatment itself."""
    if text is None:
        return text
    text = _LITERAL_NEWLINE_RE.sub("\n", text)
    escaped = html.escape(text, quote=False)
    escaped = _FENCE_RE.sub(lambda m: f"<pre>{m.group(1).strip()}</pre>", escaped)
    escaped = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", escaped)
    escaped = _BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    return escaped
