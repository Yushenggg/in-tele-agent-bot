# TeleBaseBot

> **⚠️ Work in Progress — This project is in development. Not for use in any hobby setting, much less production**

TeleBaseBot is a lightweight Telegram bot foundation designed to be extended.
It includes a small Telegram command pipeline, role-based access checks, basic session memory, and a pluggable agent layer powered by an OpenAI-compatible model backend.

The goal of this project is not to be a finished product. It is a usable base bot you can build on:

- add more Telegram commands and handlers
- expand the role and permission model
- swap or improve the agent backend
- change the system prompt and toolset
- add persistence, analytics, or admin controls

## Requirements

- Python 3.12 or newer
- `uv` for dependency management and running the project
- a Telegram bot token from BotFather
- an OpenAI-compatible model endpoint, or another compatible chat backend

## Install `uv`

### macOS and Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart your shell after installation if `uv` is not immediately available.

### Windows

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Verify installation

```bash
uv --version
```

## Project Setup

Clone the repository and install dependencies:

```bash
git clone <your-repo-url>
cd TeleBaseBot
uv sync
```

If you prefer to run it without creating a separate virtual environment manually, `uv` will manage the environment for you.

## Configuration

Create a `.env` file in the project root. Use `.env.example` as the template.

Minimum required values:

```dotenv
TELEGRAM_TOKEN=your-telegram-bot-token
ADMIN_USERS=[123456789]
ALLOWED_USERS=[123456789,987654321]
OPENAI_BASE_URL=http://localhost:11434
OPENAI_API_KEY=
DEFAULT_MODEL=
```

Notes:

- `OPENAI_BASE_URL` should point to the host of your OpenAI-compatible server.
- The application normalizes this to the client API path when needed.
- `ADMIN_USERS` and `ALLOWED_USERS` are JSON-style lists of Telegram user IDs.

## Run the Bot

Start the bot with:

```bash
uv run python main.py
```

If you want to run the agent module directly during development, you can also import and instantiate it in Python to verify the model connection.

## Project Structure

```text
main.py                         # Entry point
config.py                       # Centralized application settings
agent_framework/                # Agent and prompt logic
telegram_bot/                   # Telegram app, middleware, commands, session store
```

## How It Works

1. Telegram updates come into the bot.
2. Middleware logs the incoming message and applies role checks.
3. Text messages are appended to a small session history.
4. The agent receives the conversation history and generates a reply.
5. The bot sends the reply back to Telegram and logs the outgoing response.

---

<small>Originally written by [github.com/Yushenggg](https://github.com/Yushenggg)</small>

