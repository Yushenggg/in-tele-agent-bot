from src.config import app_config
from src.telegram_bot.app import TelegramApp

if __name__ == "__main__":
    app = TelegramApp(token=app_config.telegram_token)
    app.run()
