import contextvars
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from langchain_openai import ChatOpenAI

logger = logging.getLogger("LLM_SESSION")

session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nanogw_session_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nanogw_user_id", default=None
)


def _inject_session_header(request: httpx.Request) -> None:
    sid = session_id_var.get()
    if sid:
        request.headers["X-Session-Id"] = sid


async def _inject_session_header_async(request: httpx.Request) -> None:
    _inject_session_header(request)


def _attach_hooks(client: httpx.Client | httpx.AsyncClient | None) -> None:
    if client is None:
        return
    hook = (
        _inject_session_header_async
        if isinstance(client, httpx.AsyncClient)
        else _inject_session_header
    )
    hooks = list(client.event_hooks.get("request", []))
    if hook in hooks:
        return
    client.event_hooks["request"] = [hook, *hooks]


def _walk_httpx_clients(obj: Any) -> list[httpx.Client | httpx.AsyncClient]:
    found: list[httpx.Client | httpx.AsyncClient] = []
    for attr in ("_client",):
        candidate = getattr(obj, attr, None)
        if isinstance(candidate, (httpx.Client, httpx.AsyncClient)):
            found.append(candidate)
    return found


def _install_hooks_on_openai_client(client: Any) -> None:
    if client is None:
        return
    for hc in _walk_httpx_clients(client):
        _attach_hooks(hc)


class SessionAwareChatOpenAI(ChatOpenAI):
    """ChatOpenAI that injects per-request session metadata into every outgoing call.

    - X-Session-Id header (custom, ignored by plain OpenAI-compatible endpoints,
      read by nanogateway via proxy.py:_extract_session_id).
    - user= field in body (standard OpenAI parameter; nanogateway reads
      body.get("user"); OpenAI uses it for abuse tracking).
    - metadata.session_id in body (standard OpenAI metadata field; nanogateway
      falls back to metadata.session_id when the header is missing).

    All three are standard / ignored-by-non-nanogateway, so this subclass is
    safe to use whether OPENAI_BASE_URL points at nanogateway or directly at
    an OpenAI-compatible endpoint.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._install_request_hooks()

    def _install_request_hooks(self) -> None:
        for attr in ("root_client", "root_async_client"):
            _install_hooks_on_openai_client(getattr(self, attr, None))

    def _augment_extra_body(self, kwargs: dict[str, Any]) -> None:
        uid = user_id_var.get()
        sid = session_id_var.get()
        if uid is None and sid is None:
            return
        existing = kwargs.get("extra_body")
        if existing is None:
            extra: dict[str, Any] = {}
        elif isinstance(existing, dict):
            extra = dict(existing)
        else:
            logger.debug("extra_body is %s; skipping session injection", type(existing))
            return
        if uid is not None:
            extra.setdefault("user", uid)
        if sid is not None:
            metadata = extra.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.setdefault("session_id", sid)
            extra["metadata"] = metadata
        kwargs["extra_body"] = extra

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._install_request_hooks()
        self._augment_extra_body(kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._install_request_hooks()
        self._augment_extra_body(kwargs)
        return await super()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


@asynccontextmanager
async def llm_session(
    session_id: str | None,
    user_id: int | None = None,
) -> AsyncIterator[None]:
    """Bind the current asyncio task's context to a nanogateway session.

    The bound contextvars are read by SessionAwareChatOpenAI on every HTTP
    request, so any LLM call — sync or async — issued within this block will
    carry the session and user identifiers.

    Pass session_id=None to clear (defensive).
    """
    sid_token = session_id_var.set(session_id)
    uid_token = (
        user_id_var.set(str(user_id)) if user_id is not None else user_id_var.set(None)
    )
    try:
        yield
    finally:
        try:
            session_id_var.reset(sid_token)
        except ValueError:
            pass
        try:
            user_id_var.reset(uid_token)
        except ValueError:
            pass
