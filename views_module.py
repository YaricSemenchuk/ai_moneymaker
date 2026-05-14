"""
👁 Views module — накрутка просмотров постов в каналах/группах.

Безопасность:
- Использует штатный RPC messages.getMessagesViews с increment=true (тот же,
  что и официальный клиент при открытии канала).
- Просмотры АНОНИМНЫ — не палятся через спам-репорты и анти-спам ML.
- Единственный реальный риск — FloodWait, обрабатывается per-agent.

Лимиты по умолчанию (можно править через env):
  VIEWS_MAX_PER_HOUR=60         # просмотров/час на агента
  VIEWS_BATCH_SIZE=20           # сколько постов за один вызов
  VIEWS_JITTER_MIN_SEC=2
  VIEWS_JITTER_MAX_SEC=8        # пауза между батчами
  VIEWS_ACTIVE_HOUR_START=10
  VIEWS_ACTIVE_HOUR_END=22
"""
import asyncio
import logging
import os
import random
import time
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("views")

VIEWS_MAX_PER_HOUR = int(os.getenv("VIEWS_MAX_PER_HOUR", "60"))
VIEWS_BATCH_SIZE = int(os.getenv("VIEWS_BATCH_SIZE", "20"))
VIEWS_JITTER_MIN = float(os.getenv("VIEWS_JITTER_MIN_SEC", "2"))
VIEWS_JITTER_MAX = float(os.getenv("VIEWS_JITTER_MAX_SEC", "8"))
VIEWS_ACTIVE_HOUR_START = int(os.getenv("VIEWS_ACTIVE_HOUR_START", "10"))
VIEWS_ACTIVE_HOUR_END = int(os.getenv("VIEWS_ACTIVE_HOUR_END", "22"))


def _in_active_hours() -> bool:
    h = datetime.now().hour
    return VIEWS_ACTIVE_HOUR_START <= h < VIEWS_ACTIVE_HOUR_END


class ViewsManager:
    """Per-agent менеджер просмотров с rate-limit."""

    def __init__(self, agent_id: int, client, db, log_prefix: str = ""):
        self.agent_id = agent_id
        self.client = client
        self.db = db
        self.log_prefix = log_prefix or f"[agent {agent_id}]"
        self._timestamps: List[float] = []

    def _allow(self) -> bool:
        now = time.time()
        self._timestamps = [t for t in self._timestamps if now - t < 3600]
        return len(self._timestamps) < VIEWS_MAX_PER_HOUR

    async def view_messages(self, chat_ref, message_ids: List[int],
                            group_db_id: Optional[int] = None) -> int:
        """
        Прокидывает RPC getMessagesViews с increment=True.
        chat_ref — username/id канала. message_ids — список ID.
        Возвращает число успешно «просмотренных» постов.
        """
        if not message_ids:
            return 0
        if not self._allow():
            logger.info(f"{self.log_prefix} views rate-limit (60/hr) reached, skip")
            return 0

        try:
            from pyrogram.raw.functions.messages import GetMessagesViews
        except Exception as e:
            logger.error(f"{self.log_prefix} pyrogram raw API unavailable: {e}")
            return 0

        total = 0
        # резолвим peer один раз
        try:
            peer = await self.client.resolve_peer(chat_ref)
        except Exception as e:
            logger.warning(f"{self.log_prefix} can't resolve peer {chat_ref}: {e}")
            return 0

        # бьём по батчам
        for i in range(0, len(message_ids), VIEWS_BATCH_SIZE):
            if not self._allow():
                break
            batch = message_ids[i:i + VIEWS_BATCH_SIZE]
            try:
                await self.client.invoke(GetMessagesViews(peer=peer, id=batch, increment=True))
                total += len(batch)
                self._timestamps.extend([time.time()] * len(batch))
                logger.info(f"{self.log_prefix} viewed {len(batch)} msgs in {chat_ref}")
            except Exception as e:
                name = type(e).__name__
                if 'FloodWait' in name:
                    wait = getattr(e, 'value', 30)
                    logger.warning(f"{self.log_prefix} FloodWait {wait}s, abort batch")
                    if group_db_id is not None:
                        self.db.log_views(self.agent_id, group_db_id, 0, status='flood')
                    return total
                logger.warning(f"{self.log_prefix} views error {name}: {e}")
                if group_db_id is not None:
                    self.db.log_views(self.agent_id, group_db_id, total, status='error')
                return total
            # джиттер между батчами
            await asyncio.sleep(random.uniform(VIEWS_JITTER_MIN, VIEWS_JITTER_MAX))

        if total and group_db_id is not None:
            self.db.log_views(self.agent_id, group_db_id, total, status='ok')
        return total

    async def view_last_n(self, chat_ref, n: int,
                          group_db_id: Optional[int] = None) -> int:
        """Получает последние n message_id через get_chat_history и просматривает их."""
        try:
            ids = []
            async for msg in self.client.get_chat_history(chat_ref, limit=n):
                ids.append(msg.id)
            return await self.view_messages(chat_ref, ids, group_db_id=group_db_id)
        except Exception as e:
            logger.warning(f"{self.log_prefix} can't list history of {chat_ref}: {e}")
            return 0


async def auto_views_pass(agent_id: int, client, db, agent_joined_groups: List[dict],
                          log_prefix: str = "", max_groups: int = 5):
    """
    Один проход авто-просмотров для агента.
    Берёт до `max_groups` случайных каналов с status=joined/active, листает 20 постов в каждом.
    Уважает рабочие часы.
    """
    if not _in_active_hours():
        logger.debug(f"{log_prefix} auto-views: outside working hours, skip")
        return

    if not db.get_agent_views_enabled(agent_id):
        return

    # отбираем каналы (где есть просмотры) — joined/active с username
    candidates = [g for g in agent_joined_groups
                  if g.get('status') in ('joined', 'active') and g.get('username')]
    if not candidates:
        return

    random.shuffle(candidates)
    selected = candidates[:max_groups]

    mgr = ViewsManager(agent_id, client, db, log_prefix=log_prefix)
    total = 0
    for g in selected:
        if not mgr._allow():
            break
        n = await mgr.view_last_n(g['username'], n=20, group_db_id=g.get('id'))
        total += n
        # пауза между каналами
        await asyncio.sleep(random.uniform(5, 30))

    if total:
        logger.info(f"{log_prefix} auto-views pass: +{total} views across {len(selected)} groups")
