import logging
import asyncio
from typing import Dict, List, Optional, Callable
from pyrogram import Client
from pyrogram.types import Chat
from pyrogram.errors import RPCError as PyrogramException
from agent_database import AgentDatabase
from config_agent import TARGET_KEYWORDS
from llm_analyzer import LLMAnalyzer

logger = logging.getLogger(__name__)


class ListeningModule:
    """Модуль для мониторинга сообщений в Telegram группах."""

    def __init__(self, client: Client, db: AgentDatabase, llm: LLMAnalyzer, agent_id: Optional[int] = None):
        self.client = client
        self.db = db
        self.llm = llm
        self.agent_id = agent_id  # для membership tracking
        self.monitoring_active = False
        self.monitored_groups: List[int] = []

    async def join_groups(self, groups: List[Dict]) -> Dict[int, bool]:
        """
        Вступает в целевые группы. Использует @username если есть, иначе chat_id.

        Перед попыткой проверяет membership: если агент уже joined/requested/failed —
        пропускает (избегаем повторных запросов).

        Args:
            groups: Список групп из БД

        Returns:
            Словарь {group_id: success}
        """
        # Фильтруем группы куда уже пытались вступить
        if self.agent_id:
            original_count = len(groups)
            groups = self.db.filter_groups_to_join(self.agent_id, groups)
            skipped = original_count - len(groups)
            if skipped > 0:
                logger.info(f"⏭️  Skipped {skipped} groups (already joined/requested/failed for agent #{self.agent_id})")

        results = {}
        success_count = 0
        fail_reasons = {}

        for group in groups:
            db_group_id = group.get("id")
            telegram_group_id = group.get("telegram_group_id")
            username = group.get("username")
            title = group.get("title", "Unknown")

            if not telegram_group_id and not username:
                logger.warning(f"⚠️  No ID or username for: {title}")
                results[db_group_id] = False
                continue

            # Используем username если есть (надёжнее)
            join_target = f"@{username}" if username else telegram_group_id

            # Helper для записи membership агента (если задан)
            def record(status: str, error_msg: Optional[str] = None):
                if self.agent_id:
                    self.db.set_membership(self.agent_id, db_group_id, status, error_msg)

            try:
                logger.info(f"🚪 Joining: {title}")
                await self.client.join_chat(join_target)

                results[db_group_id] = True
                self.monitored_groups.append(telegram_group_id)
                success_count += 1
                logger.info(f"   ✅ Successfully joined")

                # Обновляем статус группы и membership агента
                self.db.update_group_status(db_group_id, "joined")
                record("joined")

            except PyrogramException as e:
                err_str = str(e)
                results[db_group_id] = False

                if "ALREADY_PARTICIPANT" in err_str or "already a participant" in err_str.lower():
                    results[db_group_id] = True
                    self.monitored_groups.append(telegram_group_id)
                    success_count += 1
                    logger.info(f"   ✅ Already in group")
                    self.db.update_group_status(db_group_id, "joined")
                    record("joined")

                elif "FLOOD_WAIT" in err_str or "floodwait" in err_str.lower():
                    wait_seconds = getattr(e, 'value', 60)
                    logger.warning(f"   ⏳ FloodWait {wait_seconds}s — STOPPING all joins")
                    fail_reasons["flood_wait"] = fail_reasons.get("flood_wait", 0) + 1
                    # FloodWait — НЕ записываем как failed, попробуем позже
                    break

                elif "INVITE_REQUEST_SENT" in err_str:
                    logger.info(f"   📨 Join request sent (waiting approval)")
                    self.db.update_group_status(db_group_id, "request_sent")
                    record("requested", err_str)
                    fail_reasons["request_sent"] = fail_reasons.get("request_sent", 0) + 1

                elif "USERNAME_INVALID" in err_str or "USERNAME_NOT_OCCUPIED" in err_str:
                    logger.warning(f"   ❌ Invalid/deleted: {join_target}")
                    self.db.update_group_status(db_group_id, "invalid")
                    record("failed", "invalid_username")
                    fail_reasons["invalid"] = fail_reasons.get("invalid", 0) + 1

                elif "CHANNEL_PRIVATE" in err_str:
                    logger.warning(f"   🔒 Private group — can't join without invite")
                    self.db.update_group_status(db_group_id, "private")
                    record("failed", "channel_private")
                    fail_reasons["private"] = fail_reasons.get("private", 0) + 1

                elif "CHANNELS_TOO_MUCH" in err_str:
                    logger.error(f"   ⛔ Too many channels — Telegram limit reached!")
                    fail_reasons["channels_too_much"] = fail_reasons.get("channels_too_much", 0) + 1
                    break

                elif "USER_BANNED_IN_CHANNEL" in err_str:
                    logger.warning(f"   ⛔ Banned in this group — blacklisting globally")
                    try:
                        self.db.blacklist_group(db_group_id, reason=err_str[:200])
                    except Exception as ex:
                        logger.debug(f"blacklist_group error: {ex}")
                        self.db.update_group_status(db_group_id, "banned")
                    record("banned", err_str)
                    fail_reasons["banned"] = fail_reasons.get("banned", 0) + 1

                else:
                    logger.warning(f"   ❌ Failed: {e}")
                    record("failed", err_str)
                    fail_reasons["other"] = fail_reasons.get("other", 0) + 1

            except Exception as e:
                results[db_group_id] = False
                logger.error(f"   ❌ Unexpected error: {e}")
                if self.agent_id:
                    self.db.set_membership(self.agent_id, db_group_id, "failed", str(e))
                fail_reasons["error"] = fail_reasons.get("error", 0) + 1

            # Пауза между вступлениями (антибан) - случайная, 20-40 сек
            # Telegram очень строг с новыми аккаунтами и быстрыми вступлениями
            import random
            await asyncio.sleep(random.uniform(20, 40))

        # Итоговая статистика
        logger.info(f"\n📊 Join summary: {success_count}/{len(groups)} succeeded")
        if fail_reasons:
            for reason, count in fail_reasons.items():
                logger.info(f"   • {reason}: {count}")

        return results

    async def leave_groups(self, group_ids: List[int]) -> Dict[int, bool]:
        """
        Выходит из групп.

        Args:
            group_ids: Список ID групп

        Returns:
            Словарь {group_id: success}
        """
        results = {}

        for group_id in group_ids:
            try:
                logger.info(f"Leaving group {group_id}")
                await self.client.leave_chat(group_id)
                results[group_id] = True
                if group_id in self.monitored_groups:
                    self.monitored_groups.remove(group_id)

            except PyrogramException as e:
                results[group_id] = False
                logger.warning(f"Failed to leave group {group_id}: {e}")

        return results

    def filter_relevant_messages(self, message_text: str, keywords: List[str] = None) -> bool:
        """
        Проверяет релевантность сообщения.

        Args:
            message_text: Текст сообщения
            keywords: Ключевые слова для фильтрации (по умолчанию из конфига)

        Returns:
            True если сообщение релевантно
        """
        if not message_text or not message_text.strip():
            return False

        kws = keywords or TARGET_KEYWORDS

        # Используем LLM анализатор для проверки релевантности
        analysis = self.llm.analyze_message(message_text)

        if analysis["interest_level"] > 0.15:
            return True

        # Дополнительная проверка по ключевым словам
        message_lower = message_text.lower()
        if any(kw.lower() in message_lower for kw in kws):
            return True

        return False

    async def listen_messages(self,
                             on_relevant_message: Callable = None,
                             timeout: int = 300) -> int:
        """
        Слушает новые сообщения в группах.

        Args:
            on_relevant_message: Callback функция для релевантных сообщений
            timeout: Таймаут в секундах

        Returns:
            Количество обработанных сообщений
        """
        if not self.monitored_groups:
            logger.warning("No groups to monitor")
            return 0

        self.monitoring_active = True
        processed_count = 0

        try:
            logger.info(f"Started listening to {len(self.monitored_groups)} groups")

            # В реальной реализации здесь будет использоваться Pyrogram handlers
            # Пример с обработчиком:
            # @self.client.on_message(filters.chat(self.monitored_groups))
            # async def handle_message(client, message):
            #     if self.filter_relevant_messages(message.text):
            #         if on_relevant_message:
            #             await on_relevant_message(message)
            #         processed_count += 1

            logger.debug("Message handlers registered")

            # Для демонстрации ждем timeout
            await asyncio.sleep(timeout)

        except Exception as e:
            logger.error(f"Error in listen_messages: {e}")

        finally:
            self.monitoring_active = False
            logger.info(f"Listening stopped. Processed {processed_count} messages")

        return processed_count

    async def process_message(self, message: Dict) -> bool:
        """
        Обрабатывает сообщение (анализирует, решает отвечать ли).

        Args:
            message: Словарь с информацией о сообщении

        Returns:
            True если нужно отправить ответ
        """
        try:
            message_text = message.get("text", "")
            message_id = message.get("message_id")
            group_id = message.get("group_id")
            user_id = message.get("user_id")

            if not message_text or not group_id:
                return False

            # Анализируем сообщение
            analysis = self.llm.analyze_message(message_text)

            # Отвечаем только если есть интерес к заработку
            if analysis["interested"] or analysis["interest_level"] > 0.3:
                logger.info(f"Relevant message found in group {group_id}: {message_text[:50]}...")
                return True

            return False

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            return False

    def get_monitoring_status(self) -> Dict:
        """Возвращает статус мониторинга."""
        return {
            "monitoring_active": self.monitoring_active,
            "groups_monitored": len(self.monitored_groups),
            "group_ids": self.monitored_groups.copy()
        }

    async def check_group_accessibility(self, group_id: int) -> bool:
        """
        Проверяет доступность группы.

        Args:
            group_id: ID группы

        Returns:
            True если группа доступна
        """
        try:
            # Пытаемся получить информацию о группе
            chat = await self.client.get_chat(group_id)
            logger.debug(f"Group {group_id} is accessible: {getattr(chat, 'title', 'Unknown')}")
            return True

        except PyrogramException as e:
            if "CHANNEL_PRIVATE" in str(e) or "CHAT_ID_INVALID" in str(e):
                logger.warning(f"Group {group_id} is no longer accessible")
                return False
            else:
                logger.error(f"Error checking group accessibility: {e}")
                return False

        except Exception as e:
            logger.error(f"Unexpected error checking group: {e}")
            return False
