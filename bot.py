"""
Personal expense-tracking Telegram bot -- now grown into Morrow, a wider
personal companion (meals, workouts, vitals, to-dos, durable memory, and
cross-domain rundowns on top of the original expense tracking).

This file is a thin facade over the real implementation, which lives in
one module per domain (finance.py, nutrition.py, fitness.py, vitals.py,
tasks.py, memory.py, rundown.py, summary.py), plus a few shared layers
(formatting.py, access.py, replies.py, correction.py, handlers.py) and the
actual entrypoint (app.py). It stays a plain module -- not a package --
and keeps re-exporting every name the test suite and `python bot.py`
depend on, specifically so neither the Procfile's `worker: python bot.py`
nor any existing `import bot; bot.<name>` test call site has to change.

Commands:
  /start                  intro + help
  /help                   command reference
  /settarget 100          set today's (and future) daily spending target
  /log 12.50 lunch        log a personal expense against today's allowance
  /log 20 USD taxi        same, but in a specific currency (converted to your base currency)
  /claim 300 groceries    log a claimable/reimbursable expense (separate pool)
  /claimed                mark all pending claimables as reimbursed, clears them
  /balance                target, balance, spent today, pending claimables, streak
  /summary [today|week|month]   AI-written spending breakdown by category
  /recent [n]             last n logged expenses with their IDs (default 10)
  /undo                   remove the single most recent expense
  /edit <id> <amount> [description...]   fix a mislogged expense
  /delete <id>            remove a specific expense by ID
  /addtask <description>  add a to-do, e.g. "call the dentist tomorrow 5pm"
  /tasks                  show the open to-do list, soonest due first
  /done <id>              mark a to-do done

You can also just type naturally, e.g. "spent 15 on uber" or
"paid 20 USD for taxi, claimable" and the bot will parse, categorize, and
log it -- asking a quick follow-up question only when something's unclear.

Run with: python bot.py
"""

from dotenv import load_dotenv
load_dotenv()  # must run before `import config` (below, transitively) -- it reads env vars at import time

import config
import db
import fx
import trends
import ai

from access import _allowed, _reject_if_not_allowed
from app import help_cmd, main, rollover_tick, start
from correction import (
    CORRECTION_ACTIONS,
    CORRECTION_UNDO_PHRASES,
    LAST_CORRECTION_KEY,
    SIMPLE_DOMAIN_ACTIONS,
    TASK_DOMAIN_ACTIONS,
    _DOMAIN_OPS,
    _handle_correction,
    _handle_simple_domain_correction,
    _revert_last_correction,
)
from finance import (
    _balance_text,
    _month_to_date_text,
    _recent_text,
    _split_amount_currency_description,
    adjustbalance_cmd,
    balance,
    claim_expense,
    claimed,
    delete_cmd,
    edit_cmd,
    log_expense,
    recent,
    settarget,
    undo,
)
from fitness import logworkout_cmd, recentworkouts
from formatting import (
    _calorie_range,
    _daily_meal_totals_text,
    _expense_line,
    _meal_line,
    _memory_line,
    _money,
    _status_text,
    _task_line,
    _vitals_line,
    _workout_line,
)
from handlers import (
    PENDING_KEY,
    RECENT_EXPENSES_FOR_AI,
    RECENT_MESSAGES_FOR_AI,
    handle_text,
    on_error,
)
from memory import _memory_for_ai, _memory_text, forget_cmd, memory_cmd
from nutrition import _log_meal_and_reply, _log_meals_and_reply, handle_photo, logmeal_cmd, recentmeals
from replies import _reply, _send_alert_if_needed
from rundown import RUNDOWN_WINDOW_DAYS, _rundown_fallback_text, _rundown_payload, _rundown_reply_text, rundown_cmd
from summary import WEEKDAY_NAMES, summary
from tasks import (
    _due_at_from_fields,
    _log_task_and_reply,
    _log_tasks_and_reply,
    _recent_tasks_for_ai,
    _tasks_text,
    addtask_cmd,
    done_cmd,
    tasks_cmd,
)
from vitals import _log_vitals_and_reply, logvitals_cmd, recentvitals

# This module no longer configures logging itself -- app.py does, since
# it's the actual entrypoint (import bot -> import app runs it already).

if __name__ == "__main__":
    main()
