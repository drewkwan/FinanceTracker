"""
Access control: this bot is meant for a single primary user (or a small
allowlist), not the general public -- every command handler checks this
before doing anything.
"""

from telegram import Update

import config


def _allowed(update: Update) -> bool:
    if not config.ALLOWED_CHAT_IDS:
        return True
    return update.effective_chat.id in config.ALLOWED_CHAT_IDS


async def _reject_if_not_allowed(update: Update) -> bool:
    if not _allowed(update):
        await update.message.reply_text("This bot is private. Ask the owner to add your chat ID.")
        return True
    return False
