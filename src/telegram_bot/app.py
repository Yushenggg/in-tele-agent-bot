import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, TypeHandler

from .auth import RoleResolver
from .commands import default_handler, start
from .middleware import LoggingMiddleware, RoleMiddleware

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Suppress verbose HTTP client logs
logging.getLogger("httpx").setLevel(logging.WARNING)

class TelegramApp:
    def __init__(self, token):
        self.application = ApplicationBuilder().token(token).concurrent_updates(True).build()
        self.logging_middleware = LoggingMiddleware()
        role_resolver = RoleResolver.from_config()
        self.role_middleware = RoleMiddleware(role_resolver=role_resolver)
        self._handlers_registered = False

    def setup_handlers(self):
        if self._handlers_registered:
            return
        self.application.add_handler(TypeHandler(Update, self.logging_middleware), group=-2)
        self.application.add_handler(TypeHandler(Update, self.role_middleware), group=-1)
        self.application.add_handler(CommandHandler('start', start))
        self.application.add_handler(TypeHandler(Update, default_handler), group=1)
        self._handlers_registered = True

    def run(self):
        self.setup_handlers()
        self.application.run_polling()


