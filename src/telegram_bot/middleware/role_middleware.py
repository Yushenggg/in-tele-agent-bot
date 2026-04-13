import logging

from pydantic import BaseModel, Field
from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..auth import RoleResolver

logger = logging.getLogger("TELEGRAM")



class RoleMiddleware(BaseModel):
    role_resolver: RoleResolver = Field(..., frozen=True)

    class Config:
        arbitrary_types_allowed = True

    async def __call__(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Temporary prototype gate: allow only admin and configured allowed users.
        if update.effective_user is None:
            return

        user_id = update.effective_user.id
        context.user_data["role"] = self.role_resolver.resolve_role(user_id)

        if self.role_resolver.can_chat(user_id):
            return

        if update.effective_chat:
            role = context.user_data.get("role", "user")
            logger.info(
                "Denied access to user_id=%s with role=%s",
                user_id,
                role,
            )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "You are not allowed to use this bot right now. "
                    f"Detected user_id={user_id}, role={role}."
                ),
            )
        raise ApplicationHandlerStop