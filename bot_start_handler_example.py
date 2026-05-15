"""ГОТОВЫЙ К КОПИПАСТЕ snippet для @moneymakerquest_bot
И для бота-привратника канала (channel gatekeeper).

Этот файл живёт в ЭТОМ репо как референс — но логику надо скопировать
в код самого бота (@moneymakerquest_bot или новый @<gatekeeper>_bot,
отдельный сервис на Railway).

Что делает:
- Перехватывает /start <payload>
- Отправляет атрибуцию в дашборд агента по HTTPS с HMAC-подписью
- Если payload — `chag{N}_g{M}[_v{V}]` (channel-gatekeeper) — после
  атрибуции отдаёт юзеру инвайт-ссылку в канал. См. cmd_start_gatekeeper.
- Иначе показывает свой обычный приветственный экран.

ENV, которые надо добавить в Railway для бота:
  TRACK_SECRET=<тот же что в env агента>
  TRACK_URL=https://<твой-агент>.up.railway.app/api/track-signup
  # только для channel-gatekeeper:
  CHANNEL_INVITE_LINK=https://t.me/+abc123   # из настроек приватного канала
  CHANNEL_PUBLIC=@moneymaker_channel         # если канал публичный, можно вместо invite

Зависимости: requests (он почти везде уже есть).
"""
import hmac
import hashlib
import json
import logging
import os
import threading
from typing import Optional

import requests

log = logging.getLogger(__name__)

TRACK_SECRET = os.getenv("TRACK_SECRET", "")
TRACK_URL = os.getenv("TRACK_URL", "")  # https://<dashboard>/api/track-signup


def _post_attribution(telegram_user_id: int, payload: Optional[str]) -> None:
    """Синхронный POST. Вызывай через _track_async — он завернёт в тред."""
    if not TRACK_SECRET or not TRACK_URL:
        return
    body = json.dumps({
        "telegram_user_id": telegram_user_id,
        "payload": payload or "",
    }, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(TRACK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Track-Signature": sig,
    }
    try:
        r = requests.post(TRACK_URL, data=body, headers=headers, timeout=5)
        if r.status_code != 200:
            log.warning(f"track-signup {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"track-signup error: {e}")


def track_signup_async(telegram_user_id: int, payload: Optional[str]) -> None:
    """Fire-and-forget: не блокируем хендлер /start.

    Вызови это в самом начале своего /start-handler, до отправки welcome.
    """
    threading.Thread(
        target=_post_attribution,
        args=(telegram_user_id, payload),
        daemon=True,
    ).start()


# ============================================================
# === Примеры интеграции в существующий /start-handler бота ===
# ============================================================

# --- aiogram 3.x ---
# from aiogram import Router
# from aiogram.types import Message
# from aiogram.filters import CommandStart, CommandObject
#
# router = Router()
#
# @router.message(CommandStart())
# async def cmd_start(msg: Message, command: CommandObject):
#     track_signup_async(msg.from_user.id, command.args)  # <-- одна строка
#     await msg.answer("Привет! Тут задания за USDT...")

# --- pyrogram / pyrofork ---
# from pyrogram import Client, filters
#
# @app.on_message(filters.command("start"))
# async def cmd_start(client, message):
#     parts = (message.text or "").split(maxsplit=1)
#     payload = parts[1] if len(parts) > 1 else ""
#     track_signup_async(message.from_user.id, payload)  # <-- одна строка
#     await message.reply("Привет! Тут задания за USDT...")

# --- python-telegram-bot v20+ ---
# from telegram import Update
# from telegram.ext import CommandHandler, ContextTypes
#
# async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
#     payload = ctx.args[0] if ctx.args else ""
#     track_signup_async(update.effective_user.id, payload)  # <-- одна строка
#     await update.message.reply_text("Привет! Тут задания за USDT...")


# ============================================================
# === CHANNEL GATEKEEPER (B2) ================================
# ============================================================
# Отдельный бот-привратник для type=channel: получает /start chag{N}_g{M}_v{V},
# трекает атрибуцию И отдаёт юзеру инвайт-ссылку в канал. Так мы получаем
# точную аттрибуцию по агенту/группе/CTA-варианту даже когда таргет — канал.
#
# CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "")  # https://t.me/+hash
# CHANNEL_PUBLIC = os.getenv("CHANNEL_PUBLIC", "")            # @moneymaker_channel
#
# def _channel_link() -> str:
#     return CHANNEL_INVITE_LINK or (f"https://t.me/{CHANNEL_PUBLIC.lstrip('@')}" if CHANNEL_PUBLIC else "")
#
# --- aiogram 3.x пример ---
# @router.message(CommandStart())
# async def cmd_start_gatekeeper(msg: Message, command: CommandObject):
#     payload = command.args or ""
#     track_signup_async(msg.from_user.id, payload)
#     link = _channel_link()
#     if payload.startswith("chag") and link:
#         await msg.answer(
#             f"Привет! Вот канал по теме — там каждый день разбор:\n{link}",
#             disable_web_page_preview=False,
#         )
#     else:
#         await msg.answer("Привет! Тут задания за USDT...")
#
# ============================================================
# Аналитика — данные доступны в дашборде агента:
#   GET /api/conversions/summary?hours=24
#   GET /api/cta-stats?hours=168   ← CTR по A/B-вариантам CTA
#   GET /groups (там колонка signups)
# Или SQL прямо в agent.db:
#   SELECT agent_id, COUNT(*) FROM signup_sources
#   WHERE agent_id IS NOT NULL GROUP BY agent_id ORDER BY 2 DESC;
#   -- CTR по варианту:
#   SELECT cta_variant, COUNT(*) FROM signup_sources
#   WHERE cta_variant IS NOT NULL GROUP BY cta_variant;
# ============================================================
