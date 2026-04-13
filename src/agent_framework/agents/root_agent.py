from functools import lru_cache
from pathlib import Path
from typing import Any

from src.config import app_config
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "system_prompt.txt"


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


def temp_tool(input: str) -> str:
    """A temporary tool that echoes the input. Replace with actual tools as needed."""
    return f"Echo: {input}"


class ConversationalAgent:
    def __init__(self) -> None:
        self.llm = ChatOpenAI(
            model=app_config.default_model,
            openai_api_key=app_config.openai_api_key,
            openai_api_base=app_config.openai_base_url_for_client,
        )
        self.agent = create_agent(
            model=self.llm,
            system_prompt=load_system_prompt(),
            tools=[temp_tool],
        )

    def invoke(self, message: str | None = None, messages: list[dict[str, str]] | None = None) -> Any:
        if messages is None:
            if message is None:
                raise ValueError("Either message or messages must be provided")
            messages = [{"role": "user", "content": message}]
        return self.agent.invoke(
            {
                "messages": messages
            }
        )

    @staticmethod
    def extract_reply(result: Any) -> str:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        if messages:
            return str(messages[-1].content)
        return str(result)
