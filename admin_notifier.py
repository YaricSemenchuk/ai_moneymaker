"""Отправка алертов в админ-бот через Telegram Bot API.

Использует прямой HTTPS-вызов sendMessage (не Pyrogram) — это user-account
сессия Pyrogram и Bot API не пересекаются, токен от бота нужен отдельно.

Дизайн:
- fire-and-forget: alert никогда не блокирует основной поток агента
  (запускается в daemon-треде с коротким таймаутом)
- анти-флуд: один и тот же ban-алерт по группе шлётся не чаще раз в N секунд
- если ADMIN_BOT_TOKEN/CHAT_ID не заданы — все вызовы no-op
"""
import logging
import threading
import time
from typing import Dict, Tuple

import requests

from config_agent import (
    ADMIN_BOT_TOKEN, ADMIN_CHAT_ID, ADMIN_ALERTS_ENABLED,
    ADMIN_BAN_ALERT_COOLDOWN_SEC,
)

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# (agent_id, group_db_id, kind) -> ts последней отправки
_last_alert: Dict[Tuple[int, int, str], float] = {}
_last_alert_lock = threading.Lock()


def _send_sync(text: str) -> bool:
    """Синхронная отправка — выполняется в daemon-треде."""
    if not ADMIN_ALERTS_ENABLED:
        return False
    url = _TG_API.format(token=ADMIN_BOT_TOKEN)
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code == 200:
            return True
        logger.warning(f"admin_notifier: sendMessage {r.status_code} — {r.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"admin_notifier: send failed — {e}")
        return False


def _send_async(text: str) -> None:
    """Запускает отправку в daemon-треде, не дожидаясь результата."""
    t = threading.Thread(target=_send_sync, args=(text,), daemon=True)
    t.start()


def _html_escape(s: str) -> str:
    if not s:
        return ""
    return (str(s).replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def notify_ban(agent_id: int, agent_label: str,
               group_db_id: int, group_title: str,
               error_code: str, last_message: str = "",
               kind: str = "ban") -> None:
    """Fire-and-forget алерт о бане/блокировке.

    kind: "ban" / "forbidden" / "slowmode" / "flood" — выбирает эмодзи и
    участвует в анти-флуд ключе (чтобы slowmode-алерт не глушил ban-алерт).
    """
    if not ADMIN_ALERTS_ENABLED:
        return

    key = (agent_id, group_db_id, kind)
    now = time.time()
    with _last_alert_lock:
        if _last_alert.get(key, 0) + ADMIN_BAN_ALERT_COOLDOWN_SEC > now:
            return
        _last_alert[key] = now

    emoji = {"ban": "⛔", "slowmode": "⏳", "forbidden": "🚫", "flood": "🌊"}.get(kind, "⚠️")
    title_safe = _html_escape(group_title[:80] if group_title else "?")
    text = (
        f"{emoji} <b>{kind.upper()}</b>\n"
        f"Агент: <code>{agent_id}</code> ({_html_escape(agent_label)})\n"
        f"Группа: {title_safe} (db_id={group_db_id})\n"
        f"Код: <code>{_html_escape(error_code)}</code>"
    )
    if last_message:
        text += f"\n\nПоследнее сообщение:\n<code>{_html_escape(last_message[:300])}</code>"

    _send_async(text)


def notify_text(text: str) -> None:
    """Произвольный текст. Fire-and-forget, для будущих отчётов."""
    if not ADMIN_ALERTS_ENABLED:
        return
    _send_async(text)


def notify_text_sync(text: str) -> bool:
    """Синхронный вариант — для команд /report где нужен результат."""
    return _send_sync(text)
