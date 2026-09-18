import contextvars
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from langchain_openai import ChatOpenAI

session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nanogw_session_id", default=None
)

_nanogateway_enabled: bool | None = None


def _use_nanogateway() -> bool:
    """Whether session headers should be injected at all.

    Resolved from AppConfig lazily (and memoized) so this module stays
    importable without Telegram credentials present. Injection is skipped in
    direct mode so a strict OpenAI-compatible endpoint sees a vanilla request.
    """
    global _nanogateway_enabled
    if _nanogateway_enabled is None:
        try:
            from core.config import app_config

            _nanogateway_enabled = app_config.use_nanogateway
        except Exception:
            _nanogateway_enabled = False
    return _nanogateway_enabled


def _inject_session_header(request: httpx.Request) -> None:
    if not _use_nanogateway():
        return
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
    """ChatOpenAI that tags each request with the current session id.

    The id travels only in the ``X-Session-Id`` request header. Gateway-style
    proxies (nanogateway) read it and rebuild the upstream headers from
    scratch, so a strict OpenAI-compatible upstream never sees it; plain
    endpoints ignore unknown headers. Nothing is written to the request body,
    which is the channel that breaks strict OpenAI-compatible servers.

    Header injection is skipped entirely unless ``USE_NANOGATEWAY`` is enabled,
    so direct mode sends a vanilla request.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._install_request_hooks()

    def _install_request_hooks(self) -> None:
        for attr in ("root_client", "root_async_client"):
            _install_hooks_on_openai_client(getattr(self, attr, None))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._install_request_hooks()
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._install_request_hooks()
        return await super()._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


@asynccontextmanager
async def llm_session(session_id: str | None) -> AsyncIterator[None]:
    """Bind the current asyncio task's context to a nanogateway session.

    The bound contextvar is read by SessionAwareChatOpenAI on every HTTP
    request, so any LLM call — sync or async — issued within this block will
    carry the session identifier.

    Pass session_id=None to clear (defensive).
    """
    sid_token = session_id_var.set(session_id)
    try:
        yield
    finally:
        try:
            session_id_var.reset(sid_token)
        except ValueError:
            pass
