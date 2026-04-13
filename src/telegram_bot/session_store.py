import asyncio

from pydantic import BaseModel, Field


Message = dict[str, str]


class SessionStore(BaseModel):
    max_messages: int = 20
    sessions: dict[int, list[Message]] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}

    async def get_messages(self, chat_id: int) -> list[Message]:
        async with asyncio.Lock():
            return list(self.sessions.get(chat_id, []))

    async def append_message(
        self, chat_id: int, role: str, content: str
    ) -> list[Message]:
        async with asyncio.Lock():
            session = self.sessions.setdefault(chat_id, [])
            session.append({"role": role, "content": content})
            if len(session) > self.max_messages:
                del session[:-self.max_messages]
            return list(session)