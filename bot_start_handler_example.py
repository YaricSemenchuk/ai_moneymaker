"""
ПРИМЕР: /start handler для @moneymakerquest_bot для атрибуции переходов.

Этот файл живёт В РЕПО САМОГО БОТА (не этого проекта-агента).
Скопируйте логику в свой код бота.

Payload формат: ag{agent_id}_g{group_db_id}
  agX_gY    — пришёл через агента X с группы Y (наш проактив/реактив)
  ag1_dm    — пришёл из личного сообщения (если будем добавлять)
  органика  — без payload (просто /start)

Пример для aiogram 3.x. Адаптируйте под свой фреймворк (python-telegram-bot,
pyrogram, telebot — логика одинаковая).
"""
from datetime import datetime
import re
import sqlite3

# В РЕПО БОТА должен быть свой sqlite/postgres для пользователей.
# Здесь — пример минимальной таблицы атрибуции:
SCHEMA = """
CREATE TABLE IF NOT EXISTS signup_sources (
    telegram_user_id INTEGER PRIMARY KEY,
    source TEXT,            -- 'ag1_g56' / 'organic' / 'dm' / ...
    agent_id INTEGER,
    promo_group_id INTEGER,
    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_signup_source ON signup_sources(source);
"""

PAYLOAD_RE = re.compile(r"^ag(\d+)_g(\d+)$")


def parse_payload(payload: str):
    """ag1_g56 → (1, 56). Иначе None."""
    if not payload:
        return None
    m = PAYLOAD_RE.match(payload.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def log_signup(db_path: str, telegram_user_id: int, payload: str | None):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    parsed = parse_payload(payload)
    if parsed:
        agent_id, group_id = parsed
        source = f"ag{agent_id}_g{group_id}"
    else:
        agent_id, group_id, source = None, None, (payload or "organic")

    # INSERT OR IGNORE — первый источник побеждает (не перезаписываем при повторных /start)
    cur.execute(
        "INSERT OR IGNORE INTO signup_sources (telegram_user_id, source, agent_id, promo_group_id) "
        "VALUES (?, ?, ?, ?)",
        (telegram_user_id, source, agent_id, group_id),
    )
    conn.commit()
    conn.close()


# === aiogram 3.x пример ===
# from aiogram import Router
# from aiogram.types import Message
# from aiogram.filters import CommandStart, CommandObject
#
# router = Router()
#
# @router.message(CommandStart())
# async def cmd_start(msg: Message, command: CommandObject):
#     payload = command.args  # часть после /start
#     log_signup("bot.db", msg.from_user.id, payload)
#     await msg.answer("Привет! Тут задания за USDT...")


# === Запрос для аналитики (запускать в БД бота): ===
ANALYTICS_QUERY = """
-- Конверсия по агентам:
SELECT agent_id, COUNT(*) AS signups
FROM signup_sources
WHERE agent_id IS NOT NULL
GROUP BY agent_id
ORDER BY 2 DESC;

-- Конверсия по группам (топ источников):
SELECT promo_group_id, COUNT(*) AS signups
FROM signup_sources
WHERE promo_group_id IS NOT NULL
GROUP BY promo_group_id
ORDER BY 2 DESC
LIMIT 20;

-- Органика vs реклама:
SELECT
    CASE WHEN agent_id IS NULL THEN 'organic' ELSE 'promo' END AS bucket,
    COUNT(*) FROM signup_sources GROUP BY bucket;
"""
