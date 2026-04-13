import logging

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger("TELEGRAM")


class LoggingMiddleware:
    async def __call__(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log incoming and outgoing messages."""
        user_id = update.effective_user.id if update.effective_user else "N/A"
        chat_id = update.effective_chat.id if update.effective_chat else "N/A"

        # Log incoming message
        if update.message and update.message.text:
            logger.info(
                "Incoming message: user_id=%s, chat_id=%s, text='%s'",
                user_id,
                chat_id,
                update.message.text,
            )

        # The response will be sent after the handlers run, so we can't log it here directly.
        # However, python-telegram-bot's default logging will capture sent messages.
        # We can enhance this by adding a custom message factory to the bot if needed,
        # but for now, we'll rely on the default logging for outgoing messages
        # and simplify this middleware to only log incoming messages.
