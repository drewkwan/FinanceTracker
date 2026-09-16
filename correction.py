"""
Corrections and undo, for every domain. Two shapes coexist here on purpose:

- Expenses support a full field-level correction surface (date, currency,
  amount, description, category, delete) -- the original, most-used case.
- Meal/workout/vitals/task corrections are intentionally narrower. Each
  domain declares its own "actions" allowlist in _DOMAIN_OPS instead of
  sharing one global set, specifically so a domain-specific restriction
  (e.g. tasks support mark_done/edit_due/delete but not edit_date -- a
  forward-looking due date can't reuse days_ago's backward-only
  "today - N days" math, so it gets its own due_in_days/due_time fields
  instead, the same forward-looking shape log_task already uses) is
  enforced here in code, not just in the prompt. _handle_simple_domain_correction
  is the one implementation all four of those domains share.
- "balance" is a fifth, even narrower shape: a single per-chat running
  number, not a row with an id -- see _handle_balance_adjustment.

Every confirmation message here is built from real values just read back
from the database -- never from AI-generated text -- so the bot can never
claim to have made a change it didn't actually make. context.chat_data
holds a snapshot of the pre-change state for the single following message,
so a bare "undo" (_revert_last_correction) can reverse exactly that one
change by replaying the snapshot, never by re-guessing.
"""

from datetime import date, timedelta

from telegram import Update
from telegram.ext import ContextTypes

import config
import db
import fx
from formatting import _meal_line, _memory_line, _money, _reminder_line, _task_line, _vitals_line, _workout_line
from replies import _reply

LAST_CORRECTION_KEY = "last_correction"

# A short reply matching one of these, sent as the very next message after a
# correction, reverts it directly -- deterministic and exact-match only (not
# a substring check), so an expense description that happens to contain the
# word "wrong" can't accidentally trigger it.
CORRECTION_UNDO_PHRASES = {
    "undo", "undo that", "undo it", "revert", "no", "nope",
    "wrong", "that's wrong", "thats wrong", "no that's wrong",
}

CORRECTION_ACTIONS = {
    "edit_date", "edit_currency", "edit_amount", "edit_description", "edit_category", "delete",
}

# Registry for the non-expense domains, which all share a narrower
# correction surface than expenses (see _handle_simple_domain_correction).
# Each domain declares its own "actions" allowlist -- meal/workout/vitals
# support edit_date + delete; task supports mark_done + edit_task + delete
# instead of edit_date. edit_task is deliberately one flexible action
# rather than one per field (edit_title/edit_due/edit_notes) -- a to-do has
# few enough fields that a single correction commonly touches more than one
# at once (e.g. "push #11 to tomorrow, I need Shardul's address" reschedules
# AND adds a note), and its due date is forward-looking (due_in_days/
# due_time, the same shape log_task already extracts for a new to-do) since
# it can't reuse edit_date's backward-only days_ago math (see ai.py's
# PARSE_SYSTEM_PROMPT correction rules).
SIMPLE_DOMAIN_ACTIONS = {"edit_date", "delete"}
TASK_DOMAIN_ACTIONS = {"mark_done", "edit_task", "delete"}

_DOMAIN_OPS = {
    "meal": {"noun": "meal", "recent_cmd": "/recentmeals", "date_field": "meal_date",
              "actions": SIMPLE_DOMAIN_ACTIONS, "actions_desc": "only moving the date or deleting one",
              "get": db.get_meal, "edit_date": db.edit_meal_date, "delete": db.delete_meal,
              "restore": db.restore_deleted_meal, "line": _meal_line},
    "workout": {"noun": "workout", "recent_cmd": "/recentworkouts", "date_field": "workout_date",
                 "actions": SIMPLE_DOMAIN_ACTIONS, "actions_desc": "only moving the date or deleting one",
                 "get": db.get_workout, "edit_date": db.edit_workout_date, "delete": db.delete_workout,
                 "restore": db.restore_deleted_workout, "line": _workout_line},
    "vitals": {"noun": "check-in", "recent_cmd": "/recentvitals", "date_field": "vitals_date",
                "actions": SIMPLE_DOMAIN_ACTIONS, "actions_desc": "only moving the date or deleting one",
                "get": db.get_vitals, "edit_date": db.edit_vitals_date, "delete": db.delete_vitals,
                "restore": db.restore_deleted_vitals, "line": _vitals_line},
    "task": {"noun": "to-do", "recent_cmd": "/tasks",
              "actions": TASK_DOMAIN_ACTIONS,
              "actions_desc": "only marking one done, editing its title/due date/notes, or deleting one",
              "get": db.get_task, "delete": db.delete_task, "restore": db.restore_deleted_task,
              "line": _task_line, "mark_done": db.mark_task_done, "unmark_done": db.unmark_task_done,
              "edit_task": db.edit_task},
}


async def _handle_simple_domain_correction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                            parsed: dict, domain: str, recent_ids: set):
    """Corrections for meal/workout/vitals/task targets are intentionally
    narrower than expenses right now -- each domain's own "actions"
    allowlist (see _DOMAIN_OPS) covers its flagship case (moving a date or
    deleting a duplicate for meal/workout/vitals; marking done or deleting
    for task) without needing a field-level /editmeal-style command yet."""
    ops = _DOMAIN_OPS[domain]
    chat_id = update.effective_chat.id
    target_id = parsed.get("target_expense_id")
    action = parsed.get("correction_action")
    noun, recent_cmd = ops["noun"], ops["recent_cmd"]

    if target_id not in recent_ids or action not in ops["actions"]:
        await _reply(
            update, chat_id,
            f"I'm not sure which {noun} you mean, or that kind of edit isn't supported yet for "
            f"{noun}s -- {ops['actions_desc']} is. Run {recent_cmd} to see recent entries."
        )
        return

    row = ops["get"](chat_id, target_id)
    if row is None:
        await _reply(update, chat_id, f"Couldn't find that {noun} anymore -- run {recent_cmd} to check.")
        return

    if action == "edit_date":
        days_ago = parsed.get("days_ago")
        if not isinstance(days_ago, int) or not (0 <= days_ago <= 14):
            await _reply(
                update, chat_id,
                f"Which day did you mean for that {noun}? (e.g. today, yesterday, or '3 days ago')"
            )
            return
        today = date.fromisoformat(db.today_str())
        new_date = today - timedelta(days=days_ago)
        old_date = row[ops["date_field"]]
        updated = ops["edit_date"](chat_id, target_id, new_date.isoformat())
        context.chat_data[LAST_CORRECTION_KEY] = {
            "domain": domain, "action": "edit_date", "expense_id": target_id, "old_date": old_date,
        }
        await _reply(
            update, chat_id,
            f"Updated -- that {noun} is now dated {updated[ops['date_field']]} (was {old_date}). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_task":
        # One flexible action rather than one per field -- title, due date,
        # and notes can each change independently, in whatever combination
        # the message actually implies (e.g. "push #11 to tomorrow, I need
        # Shardul's address" reschedules AND adds a note in one correction).
        # A due date is forward-looking (due_in_days/due_time), unlike
        # edit_date's backward-only days_ago -- "in 3 days" can't be
        # expressed as "N days ago".
        new_title = parsed.get("new_description")
        new_notes = parsed.get("new_task_notes")
        due_in_days = parsed.get("due_in_days")
        new_due_at = None
        if isinstance(due_in_days, int) and due_in_days >= 0:
            due_time = parsed.get("due_time")
            today = date.fromisoformat(db.today_str())
            due_date = today + timedelta(days=due_in_days)
            new_due_at = f"{due_date.isoformat()} {due_time}" if due_time else due_date.isoformat()

        if new_title is None and new_notes is None and new_due_at is None:
            await _reply(
                update, chat_id,
                f"What should I change about that {noun} -- the title, the due date, or a note?"
            )
            return

        # Only pass fields actually changing -- db.edit_task's _UNSET default
        # leaves everything else untouched (see its docstring for why that's
        # not the same as passing None, which explicitly clears a field).
        edits = {}
        if new_title is not None:
            edits["new_title"] = new_title
        if new_due_at is not None:
            edits["new_due_at"] = new_due_at
        if new_notes is not None:
            edits["new_notes"] = new_notes

        old_title, old_due_at, old_notes = row["title"], row["due_at"], row["notes"]
        updated = ops["edit_task"](chat_id, target_id, **edits)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "domain": domain, "action": "edit_task", "expense_id": target_id,
            "old_title": old_title, "old_due_at": old_due_at, "old_notes": old_notes,
        }
        bits = []
        if new_title is not None:
            bits.append(f"now titled \"{updated['title']}\"")
        if new_due_at is not None:
            bits.append(f"due {updated['due_at']}")
        if new_notes is not None:
            bits.append("notes updated")
        await _reply(update, chat_id, f"Updated -- {', '.join(bits)}. Reply 'undo' if that's wrong.")
        return

    if action == "mark_done":
        updated = ops["mark_done"](chat_id, target_id)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "domain": domain, "action": "mark_done", "expense_id": target_id,
        }
        await _reply(update, chat_id, f"Marked done: {ops['line'](updated)}. Reply 'undo' if that's wrong.")
        return

    if action == "delete":
        deleted = ops["delete"](chat_id, target_id)
        context.chat_data[LAST_CORRECTION_KEY] = {"domain": domain, "action": "delete", "row": deleted}
        await _reply(update, chat_id, f"Deleted: {ops['line'](deleted)}. Reply 'undo' if that's wrong.")
        return


async def _handle_balance_adjustment(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict):
    """"Adjust my rolled-over balance/deficit by X" -- the one natural-
    language path onto db.adjust_balance's manual escape hatch (see its
    docstring). Unlike every other correction here, there's no recent-item
    list to match against -- balance is a single per-chat value, not a row
    -- so this only needs the delta itself, taken from the user's own words,
    never estimated or invented (same discipline as edit_amount)."""
    chat_id = update.effective_chat.id
    delta = parsed.get("new_amount")
    if not isinstance(delta, (int, float)) or delta == 0:
        await _reply(
            update, chat_id,
            "By how much should I adjust your rolled-over balance? (e.g. \"-1135.89\" to add that deficit, "
            "or a positive number to add a credit)"
        )
        return
    result = db.adjust_balance(chat_id, float(delta))
    context.chat_data[LAST_CORRECTION_KEY] = {
        "domain": "balance", "action": "adjust_balance", "old_balance": result["old_balance"],
    }
    sign = "+" if delta >= 0 else ""
    await _reply(
        update, chat_id,
        f"Balance adjusted by {sign}{_money(delta)}: {_money(result['old_balance'])} -> "
        f"{_money(result['new_balance'])}. Reply 'undo' if that's wrong."
    )


async def _handle_correction(update: Update, context: ContextTypes.DEFAULT_TYPE, parsed: dict,
                              recent_ids: set, recent_meal_ids: set = frozenset(),
                              recent_workout_ids: set = frozenset(), recent_vitals_ids: set = frozenset(),
                              recent_task_ids: set = frozenset()):
    """Applies a correction the AI identified against one of the chat's
    recent expenses/meals/workouts/vitals/tasks. Every confirmation message
    here is built from real values just read back from the database --
    never from AI-generated text -- so the bot can never claim to have made
    a change it didn't actually make (the exact failure mode that prompted
    this feature)."""
    chat_id = update.effective_chat.id
    target_id = parsed.get("target_expense_id")
    action = parsed.get("correction_action")
    domain = parsed.get("target_domain") or "expense"

    if domain == "meal":
        await _handle_simple_domain_correction(update, context, parsed, "meal", recent_meal_ids)
        return
    if domain == "workout":
        await _handle_simple_domain_correction(update, context, parsed, "workout", recent_workout_ids)
        return
    if domain == "vitals":
        await _handle_simple_domain_correction(update, context, parsed, "vitals", recent_vitals_ids)
        return
    if domain == "task":
        await _handle_simple_domain_correction(update, context, parsed, "task", recent_task_ids)
        return

    if domain == "balance":
        await _handle_balance_adjustment(update, context, parsed)
        return

    if target_id not in recent_ids or action not in CORRECTION_ACTIONS:
        await _reply(
            update, chat_id,
            "I'm not sure which expense you mean -- run /recent to see IDs, then use /edit <id> or /delete <id>."
        )
        return

    row = db.get_expense(chat_id, target_id)
    if row is None:
        await _reply(update, chat_id, "Couldn't find that expense anymore -- run /recent to check.")
        return

    if action == "edit_date":
        days_ago = parsed.get("days_ago")
        if not isinstance(days_ago, int) or not (0 <= days_ago <= 14):
            await _reply(
                update, chat_id,
                f"Which day did you mean for {_money(row['amount'], row['currency'])} -- "
                f"{row['description']}? (e.g. today, yesterday, or '3 days ago')"
            )
            return
        today = date.fromisoformat(db.today_str())
        new_date = today - timedelta(days=days_ago)
        old_date = row["expense_date"]
        updated = db.edit_expense_date(chat_id, target_id, new_date.isoformat())
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_date", "expense_id": target_id, "old_date": old_date,
        }
        await _reply(
            update, chat_id,
            f"Updated -- {_money(updated['amount'], updated['currency'])} \"{updated['description']}\" is "
            f"now dated {updated['expense_date']} (was {old_date}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_currency":
        new_currency = fx.normalize_currency(parsed.get("new_currency"))
        if not new_currency:
            await _reply(
                update, chat_id,
                f"What currency should {_money(row['amount'], row['currency'])} -- "
                f"{row['description']} actually be?"
            )
            return
        old_currency = row["currency"]
        old_display = _money(row["amount"], old_currency)
        updated = db.edit_expense(chat_id, target_id, new_currency=new_currency)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_currency", "expense_id": target_id, "old_currency": old_currency,
        }
        await _reply(
            update, chat_id,
            f"Updated -- \"{updated['description']}\" is now {_money(updated['amount'], updated['currency'])} "
            f"(was {old_display}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_amount":
        new_amount = parsed.get("new_amount")
        if not isinstance(new_amount, (int, float)) or new_amount <= 0:
            await _reply(update, chat_id, f"What should the amount for \"{row['description']}\" actually be?")
            return
        old_display = _money(row["amount"], row["currency"])
        updated = db.edit_expense(chat_id, target_id, new_amount=float(new_amount))
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_amount", "expense_id": target_id, "old_amount": row["amount"],
        }
        await _reply(
            update, chat_id,
            f"Updated -- \"{updated['description']}\" is now {_money(updated['amount'], updated['currency'])} "
            f"(was {old_display}). Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_description":
        new_description = parsed.get("new_description")
        if not new_description:
            await _reply(update, chat_id, "What should the description say instead?")
            return
        old_description = row["description"]
        updated = db.edit_expense(chat_id, target_id, new_description=new_description)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_description", "expense_id": target_id, "old_description": old_description,
        }
        await _reply(
            update, chat_id,
            f"Updated -- description is now \"{updated['description']}\" (was \"{old_description}\"). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "edit_category":
        new_category = parsed.get("new_category")
        if new_category not in config.CATEGORIES:
            await _reply(
                update, chat_id,
                f"What category should \"{row['description']}\" actually be? "
                f"({', '.join(config.CATEGORIES)})"
            )
            return
        old_category = row["category"]
        updated = db.edit_expense(chat_id, target_id, new_category=new_category)
        context.chat_data[LAST_CORRECTION_KEY] = {
            "action": "edit_category", "expense_id": target_id, "old_category": old_category,
        }
        await _reply(
            update, chat_id,
            f"Updated -- \"{updated['description']}\" is now [{updated['category']}] (was [{old_category}]). "
            "Reply 'undo' if that's wrong."
        )
        return

    if action == "delete":
        deleted = db.delete_expense(chat_id, target_id)
        context.chat_data[LAST_CORRECTION_KEY] = {"action": "delete", "row": deleted}
        await _reply(
            update, chat_id,
            f"Deleted: {_money(deleted['amount'], deleted['currency'])} -- {deleted['description']} "
            f"[{deleted['category']}] ({deleted['expense_date']}). Reply 'undo' if that's wrong."
        )
        return


async def _revert_last_correction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if there was something to revert (and replies about it),
    False if there was no pending correction snapshot at all."""
    snap = context.chat_data.pop(LAST_CORRECTION_KEY, None)
    if not snap:
        return False
    chat_id = update.effective_chat.id
    action = snap["action"]
    domain = snap.get("domain", "expense")

    if domain == "memory":
        # Memory undo is simpler than the other domains -- always a
        # delete-then-restore by label, never a date edit -- so it gets its
        # own branch instead of forcing it into the expense_id-shaped
        # _DOMAIN_OPS registry.
        if action == "delete":
            restored = db.restore_deleted_memory(chat_id, snap["row"])
            await _reply(update, chat_id, f"Restored: {_memory_line(restored)}")
        return True

    if domain == "balance":
        db.set_balance(chat_id, snap["old_balance"])
        await _reply(update, chat_id, f"Reverted -- balance is back to {_money(snap['old_balance'])}.")
        return True

    if domain == "reminder":
        # Slash-command-only right now (/donereminder, /removereminder --
        # see reminders.py's module docstring for why), so this is its own
        # small branch rather than living in _DOMAIN_OPS, the same reason
        # "memory" above gets one instead of forcing itself into that
        # id-set-matching, AI-correction-driven registry.
        if action == "delete":
            restored = db.restore_deleted_reminder(chat_id, snap["row"])
            await _reply(update, chat_id, f"Restored: {_reminder_line(restored)}")
        elif action == "mark_done":
            row = db.unmark_reminder_done_today(chat_id, snap["reminder_id"])
            await _reply(update, chat_id, f"Reverted -- back to not done today: {_reminder_line(row)}")
        return True

    if domain in _DOMAIN_OPS:
        # meal/workout/vitals/task all share the same narrow revert shape --
        # see _handle_simple_domain_correction for why their correction
        # surface is smaller than expenses'.
        ops = _DOMAIN_OPS[domain]
        if action == "delete":
            restored = ops["restore"](chat_id, snap["row"])
            await _reply(update, chat_id, f"Restored: {ops['line'](restored)}")
        elif action == "edit_date":
            row = ops["edit_date"](chat_id, snap["expense_id"], snap["old_date"])
            await _reply(update, chat_id, f"Reverted -- {ops['noun']} is back to {row[ops['date_field']]}.")
        elif action == "mark_done":
            row = ops["unmark_done"](chat_id, snap["expense_id"])
            await _reply(update, chat_id, f"Reverted -- back to open: {ops['line'](row)}")
        elif action == "edit_task":
            # Always pass all three explicitly, even where a value is None
            # (e.g. it had no due date before) -- db.edit_task's _UNSET
            # default is for "don't touch", not for these real prior values,
            # so a real None here correctly clears the field back to no-date/
            # no-notes rather than leaving whatever the edit just set.
            row = ops["edit_task"](chat_id, snap["expense_id"], new_title=snap["old_title"],
                                    new_due_at=snap["old_due_at"], new_notes=snap["old_notes"])
            await _reply(update, chat_id, f"Reverted -- back to \"{row['title']}\".")
        return True

    if action == "delete":
        restored = db.restore_deleted_expense(chat_id, snap["row"])
        await _reply(
            update, chat_id,
            f"Restored: {_money(restored['amount'], restored['currency'])} -- {restored['description']} "
            f"[{restored['category']}] ({restored['expense_date']})"
        )
    elif action == "edit_date":
        row = db.edit_expense_date(chat_id, snap["expense_id"], snap["old_date"])
        await _reply(update, chat_id, f"Reverted -- date is back to {row['expense_date']}.")
    elif action == "edit_currency":
        row = db.edit_expense(chat_id, snap["expense_id"], new_currency=snap["old_currency"])
        await _reply(update, chat_id, f"Reverted -- currency is back to {_money(row['amount'], row['currency'])}.")
    elif action == "edit_amount":
        row = db.edit_expense(chat_id, snap["expense_id"], new_amount=snap["old_amount"])
        await _reply(update, chat_id, f"Reverted -- amount is back to {_money(row['amount'], row['currency'])}.")
    elif action == "edit_description":
        row = db.edit_expense(chat_id, snap["expense_id"], new_description=snap["old_description"])
        await _reply(update, chat_id, f"Reverted -- description is back to \"{row['description']}\".")
    elif action == "edit_category":
        row = db.edit_expense(chat_id, snap["expense_id"], new_category=snap["old_category"])
        await _reply(update, chat_id, f"Reverted -- category is back to [{row['category']}].")
    return True
