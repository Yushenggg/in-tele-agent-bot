import asyncio
import logging
from typing import Literal, TypedDict

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from src.agent_framework.agents.root_agent import ConversationalAgent
from ..session_store import SessionStore

logger = logging.getLogger("TELEGRAM")


_agent = ConversationalAgent()
_session_store = SessionStore(max_messages=20)


class OutboundPayload(TypedDict):
	type: Literal["text", "sticker", "typing"]
	content: str


def _invoke_agent(messages: list[dict[str, str]]) -> str:
	logger.info(
		"agent_invoke: Received message: `%s` from user",
		messages[-1].get('content', '')
	)
	result = _agent.invoke(messages=messages)
	reply = _agent.extract_reply(result)
	logger.info(
		"agent_reply: generated response preview=%r",
		reply[:200],
	)
	return reply


def _build_text_payload(text: str) -> OutboundPayload:
	return {"type": "text", "content": text}


def _build_sticker_payload(sticker_id: str) -> OutboundPayload:
	return {"type": "sticker", "content": sticker_id}


def _build_typing_payload() -> OutboundPayload:
	return {"type": "typing", "content": "typing"}


async def default_send(
	update: Update,
	context: ContextTypes.DEFAULT_TYPE,
	payload_queue: list[OutboundPayload],
) -> None:
	if not update.effective_chat:
		return

	chat_id = update.effective_chat.id
	user_id = update.effective_user.id if update.effective_user else None

	while payload_queue:
		payload = payload_queue.pop(0)
		payload_type = payload.get("type")
		content = payload.get("content", "")

		if payload_type == "text":
			logger.info(
				"telegram_send: user_id=%s chat_id=%s type=text preview=%r",
				user_id,
				chat_id,
				content[:200],
			)
			await context.bot.send_message(chat_id=chat_id, text=content)
		elif payload_type == "sticker":
			logger.info(
				"telegram_send: user_id=%s chat_id=%s type=sticker sticker_id=%s",
				user_id,
				chat_id,
				content,
			)
			await context.bot.send_sticker(chat_id=chat_id, sticker=content)
		elif payload_type == "typing":
			logger.info(
				"telegram_send: user_id=%s chat_id=%s type=typing",
				user_id,
				chat_id,
			)
			await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)


async def _typing_pulse(
	update: Update,
	context: ContextTypes.DEFAULT_TYPE,
	stop_event: asyncio.Event,
) -> None:
	while not stop_event.is_set():
		await default_send(update, context, [_build_typing_payload()])
		try:
			await asyncio.wait_for(stop_event.wait(), timeout=4.0)
		except TimeoutError:
			continue


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
	if not update.effective_chat:
		return

	chat_id = update.effective_chat.id
	message = update.effective_message.text if update.effective_message else ""
	history = await _session_store.append_message(chat_id=chat_id, role="user", content=message)
	typing_stop = asyncio.Event()
	typing_task = asyncio.create_task(_typing_pulse(update, context, typing_stop))
	try:
		reply = await asyncio.to_thread(_invoke_agent, history)
	except Exception:
		logger.exception("Failed to invoke agent")
		reply = "Sorry, I had trouble processing that. Please try again."
		logger.info("agent_reply: using fallback response preview=%r", reply[:200])
	finally:
		typing_stop.set()
		await typing_task
	await _session_store.append_message(chat_id=chat_id, role="assistant", content=reply)

	await default_send(update, context, [_build_text_payload(reply)])


def _get_update_kind(update: Update) -> str:
	if update.effective_message is None:
		return "unknown"

	message = update.effective_message
	if message.text:
		return "text"
	if message.photo:
		return "photo"
	if message.sticker:
		return "sticker"
	if message.document:
		return "document"
	if message.audio:
		return "audio"
	if message.video:
		return "video"
	if message.voice:
		return "voice"
	if message.location:
		return "location"
	if message.contact:
		return "contact"
	if message.poll:
		return "poll"
	if message.caption:
		return "caption"
	return "other"


async def default_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
	match _get_update_kind(update):
		case "text":
			await handle_message(update, context)
		case "photo":
			if update.effective_chat:
				await default_send(
					update,
					context,
					[_build_text_payload("Nice photo! Unfortunately I can't do anything with it yet.")],
				)
		case "sticker":
			if update.effective_chat:
				payloads: list[OutboundPayload] = [
					_build_text_payload("Nice sticker! Let me send it back to you."),
					_build_sticker_payload(update.effective_message.sticker.file_id),
				]
				await default_send(update, context, payloads)
		case "document":
			if update.effective_chat:
				await default_send(update, context, [_build_text_payload("A document? I don't want that.")])
		case "audio" | "video" | "voice":
			if update.effective_chat:
				await default_send(
					update,
					context,
					[_build_text_payload("I see no evil, hear no evil. So I'm going to ignore that.")],
				)
		case "location" | "contact" | "poll":
			if update.effective_chat:
				await default_send(
					update,
					context,
					[_build_text_payload("Seems complicated. Not gonna comment on that")],
				)
		case "caption":
			if update.effective_chat:
				await default_send(update, context, [_build_text_payload("How did you get here?")])
		case _:
			if update.effective_chat:
				await default_send(update, context, [_build_text_payload("Da hell is that?")])
