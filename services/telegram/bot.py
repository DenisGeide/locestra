from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from services.config import get_settings

SETTINGS = get_settings()
GATEWAY = SETTINGS.telegram_gateway_url
VOICE = SETTINGS.telegram_voice_url
GATEWAY_AUTH = SETTINGS.gateway_credential.get_secret_value()


def gateway_headers() -> dict[str, str]:
    if not GATEWAY_AUTH:
        raise RuntimeError("The local gateway credential is unavailable")
    return {"Authorization": f"Bearer {GATEWAY_AUTH}"}


async def ask_agent(content: str | list[dict]) -> str:
    payload = {"model": "local-agent-auto", "messages": [{"role": "user", "content": content}]}
    async with httpx.AsyncClient(timeout=1800) as client:
        response = await client.post(GATEWAY, json=payload, headers=gateway_headers())
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def send_long(update: Update, message_text: str) -> None:
    message = update.effective_message
    if not message:
        return
    for start_index in range(0, len(message_text), 4000):
        await message.reply_text(message_text[start_index : start_index + 4000])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Локальный AI-агент готов. Отправьте текст, голосовое сообщение или изображение."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.chat.send_action(ChatAction.TYPING)
    try:
        await send_long(update, await ask_agent(message.text))
    except Exception as exc:
        await message.reply_text(f"Ошибка локального агента: {exc}")


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = message.voice or message.audio
    await message.chat.send_action(ChatAction.TYPING)
    suffix = Path(getattr(media, "file_name", "voice.ogg") or "voice.ogg").suffix or ".ogg"
    temp_path: Path | None = None
    try:
        telegram_file = await context.bot.get_file(media.file_id)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            temp_path = Path(tmp.name)
        await telegram_file.download_to_drive(temp_path)
        async with httpx.AsyncClient(timeout=1800) as client:
            with temp_path.open("rb") as source:
                response = await client.post(
                    VOICE,
                    files={"file": (temp_path.name, source)},
                    headers=gateway_headers(),
                )
            response.raise_for_status()
            transcript = response.json()["text"]
        answer = await ask_agent(transcript)
        await send_long(update, f"Распознано: {transcript}\n\n{answer}")
    except Exception as exc:
        await message.reply_text(f"Ошибка распознавания: {exc}")
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.chat.send_action(ChatAction.TYPING)
    try:
        telegram_file = await context.bot.get_file(message.photo[-1].file_id)
        data = bytes(await telegram_file.download_as_bytearray())
        content = [
            {"type": "text", "text": message.caption or "Опиши и проанализируй изображение."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}},
        ]
        await send_long(update, await ask_agent(content))
    except Exception as exc:
        await message.reply_text(f"Ошибка анализа изображения: {exc}")


def main() -> None:
    credential = SETTINGS.telegram_credential.get_secret_value().strip()
    if not credential:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty")
    application: Application = ApplicationBuilder().token(credential).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    application.add_handler(MessageHandler(filters.PHOTO, on_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
