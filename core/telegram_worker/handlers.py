import asyncio
import logging
import shutil
from typing import Literal

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from core.agents import verify_working_code
from core.config import app_config, WORKING_DIR, BACKUP_DIR
from core.core_agent import CoreAgent
from core.dependency_sync import (
    revert_project_files,
    snapshot_project_files,
    sync_dependencies,
)
from core.llm_session import llm_session
from core.session_manager import SessionManager
from core.telegram_worker.auth import RoleResolver

logger = logging.getLogger("HANDLERS")

_FINALIZE_SPEC_PROMPT = (
    "The user has confirmed the plan. Produce the final implementation spec for "
    "the code agent as plain text, with these sections:\n"
    "Feature, Trigger, Input, Output, Edge cases.\n"
    "Include concrete examples and exact wording the code agent will need.\n"
    "Do NOT ask questions, do NOT end with a confirmation prompt, and do NOT "
    "mention implementation details (files, imports, libraries)."
)

UpdateKind = Literal[
    "text", "photo", "sticker", "document", "audio",
    "video", "voice", "location", "contact", "poll",
    "caption", "other", "unknown",
]


def _get_update_kind(update: Update) -> UpdateKind:
    msg = update.effective_message
    if msg is None:
        return "unknown"
    if msg.text:
        return "text"
    if msg.photo:
        return "photo"
    if msg.sticker:
        return "sticker"
    if msg.document:
        return "document"
    if msg.audio:
        return "audio"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.location:
        return "location"
    if msg.contact:
        return "contact"
    if msg.poll:
        return "poll"
    if msg.caption:
        return "caption"
    return "other"


class BotHandlers:
    def __init__(self, agent: CoreAgent, auth: RoleResolver, reload_callback=None):
        self.agent = agent
        self.auth = auth
        self._reload_callback = reload_callback
        self.sessions = SessionManager()

    # ── Top-level dispatchers ──────────────────────────────────────

    async def handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.effective_chat:
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="I'm a bot, please talk to me!",
        )

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.effective_chat or not update.effective_message:
            return
        if not self.auth.is_authorized(
            update.effective_user.id if update.effective_user else None,
        ):
            return

        chat_id = update.effective_chat.id
        kind = _get_update_kind(update)

        edit_state = await self.sessions.get_edit_state(chat_id)
        if edit_state:
            if edit_state.phase == "planning":
                await self._handle_planning_response(chat_id, update, context)
            elif edit_state.phase == "executing_paused":
                await self._handle_paused_response(chat_id, update, context)
            return

        match kind:
            case "text":
                await self._handle_text(chat_id, update, context)
            case "photo":
                await self._reply(chat_id, context, "Nice photo! Unfortunately I can't do anything with it yet.")
            case "sticker":
                await self._reply(chat_id, context, "Nice sticker!")
                if update.effective_message and update.effective_message.sticker:
                    await context.bot.send_sticker(
                        chat_id=chat_id,
                        sticker=update.effective_message.sticker.file_id,
                    )
            case "document":
                await self._reply(chat_id, context, "A document? I don't want that.")
            case "audio" | "video" | "voice":
                await self._reply(chat_id, context, "I see no evil, hear no evil. So I'm going to ignore that.")
            case "location" | "contact" | "poll":
                await self._reply(chat_id, context, "Seems complicated. Not gonna comment on that.")
            case "caption":
                await self._reply(chat_id, context, "How did you get here?")
            case _:
                await self._reply(chat_id, context, "Da hell is that?")

    async def handle_edit(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        if not update.effective_chat or not update.effective_message:
            return
        if not self.auth.is_authorized(
            update.effective_user.id if update.effective_user else None,
        ):
            return

        chat_id = update.effective_chat.id
        instruction = update.effective_message.text.replace("/edit", "", 1).strip()

        if not instruction:
            await self._reply(
                chat_id, context,
                "Usage: /edit <description of what to create or modify>",
            )
            return

        await self._reply(chat_id, context, "✏️ Working on it...")

        history = await self.sessions.append_message(chat_id, "user", f"/edit {instruction}")

        plan_sid = await self.sessions.get_or_create_plan_session_id(chat_id)
        async with llm_session(plan_sid):
            plan = await self._with_typing(
                chat_id, context,
                self.agent.ainvoke_planner(history),
            )
        if plan is None:
            await self._reply(chat_id, context, "❌ Planning failed.")
            return

        history.append({"role": "assistant", "content": plan})
        await self.sessions.set_edit_state(
            chat_id,
            phase="planning",
            instruction=instruction,
            planner_history=list(history),
        )
        await self._reply(chat_id, context, plan)

    # ── Text processing (Standard Agent) ────────────────────────────

    async def _handle_text(
        self, chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        text = update.effective_message.text
        history = await self.sessions.append_message(chat_id, "user", text)

        chat_sid = await self.sessions.get_or_create_chat_session_id(chat_id)
        async with llm_session(chat_sid):
            reply = await self._with_typing(
                chat_id, context,
                self.agent.ainvoke_standard(history),
            )
        if reply is None:
            reply = "Sorry, I had trouble processing that. Please try again."

        history.append({"role": "assistant", "content": reply})
        await self.sessions.append_message(chat_id, "assistant", reply)
        await self._reply(chat_id, context, reply)

    # ── /edit planning loop ─────────────────────────────────────────

    async def _handle_planning_response(
        self, chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        state = await self.sessions.get_edit_state(chat_id)
        if state is None:
            await self._reply(chat_id, context, "⏰ Planning session timed out. Start a new /edit if needed.")
            return

        history = list(state.planner_history)
        user_text = update.effective_message.text
        history.append({"role": "user", "content": user_text})

        if user_text.strip().lower() in ("go", "execute", "yes", "confirmed", "do it", "proceed"):
            await self.sessions.clear_edit_state(chat_id)
            await self._reply(chat_id, context, "⚙️ Executing code mutation...")

            plan_sid = await self.sessions.get_or_create_plan_session_id(chat_id)
            spec_history = list(history)
            spec_history.append({"role": "user", "content": _FINALIZE_SPEC_PROMPT})
            async with llm_session(plan_sid):
                spec = await self._with_typing(
                    chat_id, context,
                    self.agent.ainvoke_planner(spec_history),
                )
            if not spec or not spec.strip():
                await self._reply(
                    chat_id, context,
                    "❌ Could not finalize the spec. No changes made.",
                )
                await self.sessions.clear_edit_session_ids(chat_id)
                return

            self._backup_working()
            project_snapshot = snapshot_project_files()

            code_sid = await self.sessions.get_or_create_code_session_id(chat_id)
            async with llm_session(code_sid):
                result = await self._with_typing(
                    chat_id, context,
                    self.agent.ainvoke_code(spec, list(state.planner_history)),
                )
            if result is None:
                self._restore_working()
                await revert_project_files(project_snapshot)
                await self.sessions.clear_edit_session_ids(chat_id)
                await self._reply(
                    chat_id, context,
                    "❌ Code Agent failed. No changes made. Rolled back.",
                )
                return

            if result.failed:
                await self.sessions.set_edit_state(
                    chat_id,
                    phase="executing_paused",
                    instruction=state.instruction,
                    planner_history=list(state.planner_history),
                    agent_history=result.history or None,
                    project_snapshot=project_snapshot,
                    spec=spec,
                )
                await self._reply(
                    chat_id, context,
                    "⚠️ " + result.reply
                    + "\n\nYour progress is saved. Reply 'continue' to try again, or 'cancel' to roll back.",
                )
                return

            if result.capped:
                await self.sessions.set_edit_state(
                    chat_id,
                    phase="executing_paused",
                    instruction=state.instruction,
                    planner_history=list(state.planner_history),
                    agent_history=result.history,
                    project_snapshot=project_snapshot,
                    spec=spec,
                )
                await self._reply(
                    chat_id, context,
                    result.reply
                    + "\n\nShould I continue? Reply 'continue' to finish, or 'cancel' to roll back.",
                )
                return

            await self._finish_execution(
                chat_id, context, result.reply, project_snapshot,
            )
        else:
            plan_sid = await self.sessions.get_or_create_plan_session_id(chat_id)
            async with llm_session(plan_sid):
                reply = await self._with_typing(
                    chat_id, context,
                    self.agent.ainvoke_planner(history),
                )
            if reply is None:
                await self._reply(
                    chat_id, context, "❌ Planning failed.",
                )
                await self.sessions.clear_edit_state(chat_id)
                await self.sessions.clear_edit_session_ids(chat_id)
                return

            history.append({"role": "assistant", "content": reply})
            await self.sessions.set_edit_state(
                chat_id,
                phase="planning",
                instruction=state.instruction,
                planner_history=history,
            )
            await self._reply(chat_id, context, reply)

    async def _finish_execution(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        code_reply: str,
        project_snapshot,
    ) -> None:
        await self.sessions.clear_edit_session_ids(chat_id)
        if not code_reply or not code_reply.strip():
            self._restore_working()
            await revert_project_files(project_snapshot)
            await self._reply(
                chat_id, context,
                "❌ Code Agent returned empty — no changes made. Rolled back.",
            )
            return

        sync = await self._sync_dependencies()
        if not sync.synced and sync.error:
            self._restore_working()
            await revert_project_files(project_snapshot)
            await self._reply(
                chat_id, context,
                f"❌ Dependency sync failed. Rolled back.\n```\n{sync.error}\n```",
            )
            return

        error = verify_working_code(WORKING_DIR)
        if error:
            self._restore_working()
            await revert_project_files(project_snapshot)
            await self._reply(
                chat_id, context,
                f"❌ Code verification failed. Rolled back.\n```\n{error}\n```",
            )
            return

        sync_note = ""
        if sync.synced or sync.kept:
            parts = []
            if sync.added:
                parts.append(f"Installed: {', '.join(sync.added)}")
            if sync.removed:
                parts.append(f"Removed: {', '.join(sync.removed)}")
            if sync.kept:
                parts.append(f"Kept (still in use): {', '.join(sync.kept)}")
            if parts:
                sync_note = f"\n\n📦 {' | '.join(parts)}"
        await self._reply(
            chat_id, context,
            f"✅ Mutation successful. Reloading in-process...{sync_note}\n\n{code_reply}",
        )
        logger.info("Mutation successful. Reloading bot components in-process.")
        if self._reload_callback:
            await self._reload_callback()
        await self._reply(chat_id, context, "🔄 Reload complete. New handlers and tools are now active.")

    async def _handle_paused_response(
        self, chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        state = await self.sessions.get_edit_state(chat_id)
        if state is None:
            await self._reply(
                chat_id, context,
                "⏰ Execution session timed out. Start a new /edit if needed.",
            )
            return

        user_text = (update.effective_message.text or "").strip().lower()
        if user_text in ("continue", "resume", "go on", "keep going", "try again", "retry"):
            await self._reply(chat_id, context, "⚙️ Continuing...")
            code_sid = await self.sessions.get_or_create_code_session_id(chat_id)
            if state.agent_history:
                async def _continue_coro():
                    async with llm_session(code_sid):
                        return await self.agent.ainvoke_code_continue(
                            state.agent_history,
                            "Continue implementing the spec. Finish all remaining work, "
                            "then reply with a brief summary.",
                        )
                coro = _continue_coro()
            else:
                async def _code_coro():
                    async with llm_session(code_sid):
                        return await self.agent.ainvoke_code(
                            state.spec or state.instruction,
                            list(state.planner_history),
                        )
                coro = _code_coro()
            result = await self._with_typing(chat_id, context, coro)
            if result is None:
                await self._reply(
                    chat_id, context,
                    "❌ Code Agent failed while continuing.",
                )
                return

            if result.failed:
                await self.sessions.set_edit_state(
                    chat_id,
                    phase="executing_paused",
                    instruction=state.instruction,
                    planner_history=state.planner_history,
                    agent_history=result.history or None,
                    project_snapshot=state.project_snapshot,
                    spec=state.spec,
                )
                await self._reply(
                    chat_id, context,
                    "⚠️ " + result.reply
                    + "\n\nYour progress is saved. Reply 'continue' to try again, or 'cancel' to roll back.",
                )
                return

            if result.capped:
                await self.sessions.set_edit_state(
                    chat_id,
                    phase="executing_paused",
                    instruction=state.instruction,
                    planner_history=state.planner_history,
                    agent_history=result.history,
                    project_snapshot=state.project_snapshot,
                    spec=state.spec,
                )
                await self._reply(
                    chat_id, context,
                    result.reply
                    + "\n\nShould I continue? Reply 'continue' to finish, or 'cancel' to roll back.",
                )
                return

            await self.sessions.clear_edit_state(chat_id)
            await self._finish_execution(
                chat_id, context, result.reply, state.project_snapshot,
            )
            return

        if user_text in ("cancel", "stop", "no", "abort"):
            self._restore_working()
            if state.project_snapshot:
                await revert_project_files(state.project_snapshot)
            await self.sessions.clear_edit_state(chat_id)
            await self.sessions.clear_edit_session_ids(chat_id)
            await self._reply(chat_id, context, "🗑️ Rolled back. No changes kept.")
            return

        await self._reply(
            chat_id, context,
            "Reply 'continue' to keep going, or 'cancel' to roll back.",
        )

    # ── Safety helpers (backup / restore) ────────────────────────────

    async def _sync_dependencies(self):
        return await sync_dependencies()

    def _backup_working(self):
        if BACKUP_DIR.exists():
            shutil.rmtree(BACKUP_DIR)
        if WORKING_DIR.exists():
            shutil.copytree(WORKING_DIR, BACKUP_DIR, dirs_exist_ok=True)

    def _restore_working(self):
        if WORKING_DIR.exists():
            shutil.rmtree(WORKING_DIR)
        if BACKUP_DIR.exists():
            shutil.copytree(BACKUP_DIR, WORKING_DIR, dirs_exist_ok=True)

    # ── Utilities ───────────────────────────────────────────────────

    @staticmethod
    async def _reply(
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ):
        if not text or not text.strip():
            text = "I didn't get a response. Please try again."

        MAX_LEN = 4000
        chunks = []
        while len(text) > MAX_LEN:
            split_at = text.rfind("\n", 0, MAX_LEN)
            if split_at == -1:
                split_at = text.rfind(" ", 0, MAX_LEN)
            if split_at == -1:
                split_at = MAX_LEN
            chunks.append(text[:split_at])
            text = text[split_at:].lstrip()
        if text:
            chunks.append(text)

        for i, chunk in enumerate(chunks):
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=chunk, parse_mode="Markdown",
                )
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=chunk)

    async def _with_typing(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        coro,
    ):
        stop = asyncio.Event()
        pulse = asyncio.create_task(self._typing_pulse(chat_id, context, stop))
        try:
            return await coro
        except Exception:
            logger.exception("Agent call failed")
            return None
        finally:
            stop.set()
            await pulse

    @staticmethod
    async def _typing_pulse(
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        stop: asyncio.Event,
    ):
        while not stop.is_set():
            await context.bot.send_chat_action(
                chat_id=chat_id, action=ChatAction.TYPING,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except TimeoutError:
                continue
