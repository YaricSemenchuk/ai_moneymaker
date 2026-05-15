import asyncio
import random
import logging
from datetime import datetime, timedelta
from typing import Dict, List
from agent_database import AgentDatabase
from config_agent import (
    MESSAGE_DELAY_MIN, MESSAGE_DELAY_MAX,
    MAX_MESSAGES_PER_HOUR, MAX_GROUPS_TO_JOIN_PER_DAY,
    AT_RISK_THRESHOLD_24H, AT_RISK_FAIL_THRESHOLD_24H,
    MAX_TOTAL_ACTIONS_PER_DAY,
)

logger = logging.getLogger(__name__)


class AntiBanManager:
    """Менеджер для защиты от банов Telegram."""

    def __init__(self, db: AgentDatabase):
        self.db = db
        self.action_log: Dict[int, List[Dict]] = {}  # agent_id -> список действий
        self._risk_warn_at: Dict[int, datetime] = {}  # throttle "at risk" warnings

    def get_delay(self, agent_id: int) -> float:
        """
        Возвращает рандомную задержку между сообщениями.

        Returns:
            Количество секунд для ожидания
        """
        # Небольшой случайный разброс в пределах установленных границ
        delay = random.uniform(MESSAGE_DELAY_MIN, MESSAGE_DELAY_MAX)
        logger.debug(f"Agent {agent_id}: delay = {delay:.1f}s")
        return delay

    def _log_action(self, agent_id: int, action: str):
        """Логирует действие агента."""
        if agent_id not in self.action_log:
            self.action_log[agent_id] = []

        self.action_log[agent_id].append({
            "action": action,
            "timestamp": datetime.now()
        })

        # Очищаем старые записи (старше 1 дня)
        cutoff = datetime.now() - timedelta(days=1)
        self.action_log[agent_id] = [
            log for log in self.action_log[agent_id]
            if log["timestamp"] > cutoff
        ]

    def can_send_message(self, agent_id: int) -> bool:
        """
        Проверяет, может ли агент отправить сообщение.

        Проверяет:
        - Лимит сообщений в час (MAX_MESSAGES_PER_HOUR)

        Returns:
            True если агент может отправить сообщение
        """
        if agent_id not in self.action_log:
            return True

        # Проверяем сообщения за последний час
        one_hour_ago = datetime.now() - timedelta(hours=1)
        recent_messages = [
            log for log in self.action_log[agent_id]
            if log["action"] == "message_sent" and log["timestamp"] > one_hour_ago
        ]

        can_send = len(recent_messages) < MAX_MESSAGES_PER_HOUR

        if not can_send:
            logger.warning(f"Agent {agent_id}: message limit reached ({MAX_MESSAGES_PER_HOUR}/hour)")
        else:
            logger.debug(f"Agent {agent_id}: can send message ({len(recent_messages)}/{MAX_MESSAGES_PER_HOUR})")

        return can_send

    def can_join_group(self, agent_id: int) -> bool:
        """
        Проверяет, может ли агент вступить в новую группу.

        Проверяет:
        - Лимит вступлений в день (MAX_GROUPS_TO_JOIN_PER_DAY)

        Returns:
            True если агент может вступить
        """
        if agent_id not in self.action_log:
            return True

        # Проверяем вступления за последний день
        one_day_ago = datetime.now() - timedelta(days=1)
        recent_joins = [
            log for log in self.action_log[agent_id]
            if log["action"] == "joined_group" and log["timestamp"] > one_day_ago
        ]

        can_join = len(recent_joins) < MAX_GROUPS_TO_JOIN_PER_DAY

        if not can_join:
            logger.warning(f"Agent {agent_id}: join limit reached ({MAX_GROUPS_TO_JOIN_PER_DAY}/day)")
        else:
            logger.debug(f"Agent {agent_id}: can join group ({len(recent_joins)}/{MAX_GROUPS_TO_JOIN_PER_DAY})")

        return can_join

    def register_message_sent(self, agent_id: int):
        """Регистрирует отправку сообщения."""
        self._log_action(agent_id, "message_sent")
        logger.info(f"Agent {agent_id}: message sent")

    def register_group_joined(self, agent_id: int):
        """Регистрирует вступление в группу."""
        self._log_action(agent_id, "joined_group")
        logger.info(f"Agent {agent_id}: joined group")

    def register_group_left(self, agent_id: int):
        """Регистрирует выход из группы."""
        self._log_action(agent_id, "left_group")
        logger.info(f"Agent {agent_id}: left group")

    def get_recent_bans_count(self, agent_id: int, hours: int = 24) -> int:
        """Сколько РЕАЛЬНЫХ банов поймал агент за последние N часов.

        Считаем только status='banned' в membership (а это записывается лишь
        при USER_BANNED_IN_CHANNEL / USER_KICKED — настоящие баны).
        SLOWMODE/FORBIDDEN/PRIVATE → status='failed' или специальные группа-статусы
        и сюда НЕ попадают.
        """
        try:
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM agent_group_membership
                WHERE agent_id = ? AND status = 'banned'
                AND last_attempt >= datetime('now', ?)
            ''', (agent_id, f'-{hours} hours'))
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def get_recent_fails_count(self, agent_id: int, hours: int = 24) -> int:
        """Сколько фейлов любого типа (interactions.status='failed') за N часов.

        Ловит CHANNEL_INVALID/SLOWMODE/EXC/CHAT_WRITE_FORBIDDEN — всё что
        get_recent_bans_count НЕ считает баном, но это всё равно сигнал
        что аккаунт спалился по поведению.
        """
        try:
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM interactions
                WHERE agent_id = ? AND status = 'failed'
                  AND created_at >= datetime('now', ?)
            ''', (agent_id, f'-{hours} hours'))
            n = cursor.fetchone()[0]
            conn.close()
            return n
        except Exception:
            return 0

    def get_total_actions_today(self, agent_id: int) -> int:
        """Сколько всего отправок (proactive + reply) сделал агент сегодня."""
        try:
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT
                  (SELECT COUNT(*) FROM interactions
                     WHERE agent_id = ? AND date(created_at) = date('now')
                       AND status IN ('sent','pending'))
                  +
                  (SELECT COUNT(*) FROM proactive_posts
                     WHERE agent_id = ? AND date(created_at) = date('now'))
            ''', (agent_id, agent_id))
            n = cursor.fetchone()[0] or 0
            conn.close()
            return n
        except Exception:
            return 0

    def has_daily_action_budget(self, agent_id: int) -> bool:
        """True если у агента осталось место в дневном лимите всех действий."""
        used = self.get_total_actions_today(agent_id)
        ok = used < MAX_TOTAL_ACTIONS_PER_DAY
        if not ok:
            last = self._risk_warn_at.get(('budget', agent_id))
            now = datetime.now()
            if last is None or (now - last) >= timedelta(hours=1):
                logger.warning(
                    f"🛑 Agent {agent_id} daily action budget exhausted: "
                    f"{used}/{MAX_TOTAL_ACTIONS_PER_DAY}"
                )
                self._risk_warn_at[('budget', agent_id)] = now
        return ok

    def is_agent_at_risk(self, agent_id: int) -> bool:
        """Проверяет, не в зоне ли риска агент.

        Два независимых триггера:
        1) Реальные баны (USER_BANNED/etc) ≥ AT_RISK_THRESHOLD_24H.
        2) Суммарные фейлы любого типа ≥ AT_RISK_FAIL_THRESHOLD_24H —
           ловит "горячие" аккаунты до того как пойдут реальные баны.
        """
        bans_24h = self.get_recent_bans_count(agent_id, hours=24)
        fails_24h = self.get_recent_fails_count(agent_id, hours=24)

        risk_by_bans = bans_24h >= AT_RISK_THRESHOLD_24H
        risk_by_fails = fails_24h >= AT_RISK_FAIL_THRESHOLD_24H

        if risk_by_bans or risk_by_fails:
            last = self._risk_warn_at.get(agent_id)
            now = datetime.now()
            if last is None or (now - last) >= timedelta(hours=1):
                reason = []
                if risk_by_bans:
                    reason.append(f"{bans_24h} bans/24h")
                if risk_by_fails:
                    reason.append(f"{fails_24h} fails/24h")
                logger.warning(f"⚠️ Agent {agent_id} at risk: {', '.join(reason)}")
                self._risk_warn_at[agent_id] = now
            return True
        return False

    async def random_human_like_behavior(self):
        """Имитирует человеческое поведение (случайные паузы и действия)."""
        pause = random.uniform(10, 60)
        logger.debug(f"Human-like pause: {pause:.1f}s")
        await asyncio.sleep(pause)

    def get_agent_status(self, agent_id: int) -> Dict:
        """Возвращает статус агента."""
        if agent_id not in self.action_log:
            return {
                "agent_id": agent_id,
                "messages_last_hour": 0,
                "joins_last_day": 0,
                "can_send": True,
                "can_join": True
            }

        one_hour_ago = datetime.now() - timedelta(hours=1)
        one_day_ago = datetime.now() - timedelta(days=1)

        messages = len([
            log for log in self.action_log[agent_id]
            if log["action"] == "message_sent" and log["timestamp"] > one_hour_ago
        ])

        joins = len([
            log for log in self.action_log[agent_id]
            if log["action"] == "joined_group" and log["timestamp"] > one_day_ago
        ])

        return {
            "agent_id": agent_id,
            "messages_last_hour": messages,
            "joins_last_day": joins,
            "can_send": messages < MAX_MESSAGES_PER_HOUR,
            "can_join": joins < MAX_GROUPS_TO_JOIN_PER_DAY
        }

    async def wait_with_delay(self, agent_id: int):
        """Ожидает с рандомной задержкой."""
        delay = self.get_delay(agent_id)
        logger.debug(f"Waiting {delay:.1f} seconds...")
        await asyncio.sleep(delay)

    def reset_logs(self, days: int = 7):
        """Очищает логи старше N дней."""
        cutoff = datetime.now() - timedelta(days=days)

        for agent_id in self.action_log:
            self.action_log[agent_id] = [
                log for log in self.action_log[agent_id]
                if log["timestamp"] > cutoff
            ]

        logger.info(f"Logs reset: keeping last {days} days")
