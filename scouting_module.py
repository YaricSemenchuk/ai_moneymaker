import logging
from typing import List, Dict, Optional
from pyrogram import Client
from pyrogram.errors import RPCError as PyrogramException, FloodWait
from pyrogram.raw import functions, types
from agent_database import AgentDatabase
from config_agent import MIN_GROUP_MEMBERS, GROUP_TITLE_BLACKLIST
from llm_analyzer import LLMAnalyzer

logger = logging.getLogger(__name__)


class ScoutingModule:
    """Модуль для поиска целевых Telegram групп."""

    def __init__(self, client: Client, db: AgentDatabase, llm: LLMAnalyzer):
        self.client = client
        self.db = db
        self.llm = llm
        self.searched_keywords: List[str] = []

    async def search_groups(self, keywords: List[str], max_results: int = 50) -> List[Dict]:
        """
        Ищет публичные Telegram группы по ключевым словам через contacts.Search.

        Args:
            keywords: Список ключевых слов для поиска
            max_results: Максимальное количество результатов на ключевое слово

        Returns:
            Список найденных групп с информацией
        """
        found_groups = []
        seen_ids = set()

        for keyword in keywords:
            try:
                logger.info(f"Searching groups for keyword: '{keyword}'")

                # Используем raw API для поиска
                result = await self.client.invoke(
                    functions.contacts.Search(
                        q=keyword,
                        limit=max_results
                    )
                )

                # Обрабатываем найденные чаты (группы и каналы)
                chats_found = 0
                for chat in result.chats:
                    try:
                        # Получаем ID
                        if hasattr(chat, 'id'):
                            chat_id = chat.id
                            if chat_id in seen_ids:
                                continue
                            seen_ids.add(chat_id)
                        else:
                            continue

                        # Это канал или мегагруппа
                        if isinstance(chat, types.Channel):
                            # Получаем информацию (broadcast=канал, megagroup=супергруппа)
                            is_broadcast = bool(getattr(chat, 'broadcast', False))
                            is_megagroup = bool(getattr(chat, 'megagroup', False))

                            telegram_id = int(f"-100{chat.id}")

                            group_info = {
                                "telegram_group_id": telegram_id,
                                "title": getattr(chat, 'title', 'Unknown'),
                                "username": getattr(chat, 'username', None),
                                "description": "",
                                "members_count": getattr(chat, 'participants_count', 0) or 0,
                                "is_megagroup": is_megagroup,
                                "is_channel": is_broadcast,  # True если канал
                                "is_public": getattr(chat, 'username', None) is not None,
                            }

                            # Только публичные с username
                            if not group_info["username"]:
                                continue

                            found_groups.append(group_info)
                            chats_found += 1
                            kind = "📢 Channel" if is_broadcast else "👥 Group"
                            logger.debug(f"  {kind}: {group_info['title']} (@{group_info['username']})")

                        elif isinstance(chat, types.Chat):
                            # Это обычная группа
                            group_info = {
                                "telegram_group_id": -chat.id,
                                "title": getattr(chat, 'title', 'Unknown'),
                                "username": None,
                                "description": "",
                                "members_count": getattr(chat, 'participants_count', 0) or 0,
                                "is_megagroup": False,
                                "is_public": False,
                            }
                            # Обычные приватные группы не добавляем
                            continue

                    except Exception as e:
                        logger.debug(f"Error processing chat: {e}")
                        continue

                logger.info(f"  → Found {chats_found} groups for '{keyword}'")

            except FloodWait as e:
                logger.warning(f"FloodWait: need to wait {e.value} seconds")
                import asyncio
                await asyncio.sleep(e.value)

            except PyrogramException as e:
                logger.error(f"Pyrogram error while searching '{keyword}': {e}")
            except Exception as e:
                logger.error(f"Error searching groups for '{keyword}': {e}")

        self.searched_keywords.extend(keywords)
        logger.info(f"Total unique groups found: {len(found_groups)}")
        return found_groups

    async def filter_group(self, group_info: Dict) -> bool:
        """
        Фильтрует группу по критериям релевантности.

        Args:
            group_info: Информация о группе

        Returns:
            True если группа подходит
        """
        # Проверка размера
        members = group_info.get("members_count", 0)
        if members < MIN_GROUP_MEMBERS:
            logger.debug(f"Group {group_info.get('title')} filtered: too few members ({members})")
            return False

        # Проверка по описанию (не спам)
        description = (group_info.get("description") or "").lower()
        title = (group_info.get("title") or "").lower()
        spam_keywords = ["scam", "fake", "обман", "мошенник"]

        for spam_kw in spam_keywords:
            if spam_kw in description or spam_kw in title:
                logger.debug(f"Group {group_info.get('title')} filtered: spam detected")
                return False

        # Опасные ниши (детские/игровые/политика) — мгновенный бан
        for bad in GROUP_TITLE_BLACKLIST:
            if bad in title or bad in description:
                logger.info(f"⛔ Group '{group_info.get('title')}' filtered: blacklisted niche '{bad}'")
                return False

        # Только публичные группы с username
        if not group_info.get("username"):
            return False

        logger.debug(f"Group {group_info.get('title')} passed filter")
        return True

    async def add_group_to_db(self, group_info: Dict) -> bool:
        """Добавляет найденную группу/канал в БД."""
        try:
            is_channel = group_info.get("is_channel", False)
            group_id = self.db.add_target_group(
                telegram_group_id=group_info.get("telegram_group_id", 0),
                title=group_info.get("title", "Unknown"),
                username=group_info.get("username"),
                description=group_info.get("description"),
                members_count=group_info.get("members_count"),
                is_channel=is_channel,
                linked_chat_id=group_info.get("linked_chat_id"),
            )

            if group_id > 0:
                kind = "📢 Channel" if is_channel else "👥 Group"
                logger.info(f"✅ Added {kind}: {group_info.get('title')} (@{group_info.get('username')}, {group_info.get('members_count')} members)")
                return True
            else:
                logger.debug(f"Already in DB: {group_info.get('title')}")
                return False

        except Exception as e:
            logger.error(f"Error adding to DB: {e}")
            return False

    def get_groups_from_db(self, status: str = "discovered") -> List[Dict]:
        """Получает группы из БД."""
        return self.db.get_target_groups(status=status, limit=100)

    async def update_group_status(self, group_id: int, status: str):
        """Обновляет статус группы."""
        try:
            self.db.update_group_status(group_id, status)
            logger.debug(f"Group {group_id} status updated to '{status}'")
        except Exception as e:
            logger.error(f"Error updating group status: {e}")

    async def add_priority_groups(self, usernames: List[str]) -> int:
        """
        Добавляет приоритетные группы по username (например @moneymaker_quest).
        Агент должен в них вступить сразу при запуске.

        Args:
            usernames: Список username (с @ или без)

        Returns:
            Количество успешно добавленных групп
        """
        added = 0
        for username in usernames:
            try:
                # Убираем @ если есть
                clean_username = username.lstrip("@")

                logger.info(f"⭐ Adding priority group: @{clean_username}")

                # Получаем информацию о группе/канале
                try:
                    chat = await self.client.get_chat(clean_username)
                except Exception as e:
                    logger.warning(f"   ❌ Cannot resolve @{clean_username}: {e}")
                    continue

                # Определяем канал это или группа
                # Pyrogram: chat.type == "channel" (broadcast) vs "supergroup"/"group"
                chat_type = getattr(chat.type, "name", "").lower() if hasattr(chat, "type") else ""
                is_channel = chat_type == "channel"
                linked_chat_id = getattr(chat, "linked_chat", None)
                if linked_chat_id:
                    linked_chat_id = getattr(linked_chat_id, "id", None)

                group_info = {
                    "telegram_group_id": chat.id,
                    "title": chat.title or f"@{clean_username}",
                    "username": clean_username,
                    "description": getattr(chat, "description", "") or "",
                    "members_count": getattr(chat, "members_count", 0) or 0,
                    "is_channel": is_channel,
                    "linked_chat_id": linked_chat_id,
                }

                if await self.add_group_to_db(group_info):
                    added += 1
                else:
                    logger.info(f"   ℹ️  Already in DB: @{clean_username}")

            except Exception as e:
                logger.error(f"   ❌ Error adding @{username}: {e}")

        logger.info(f"⭐ Priority groups added: {added}/{len(usernames)}")
        return added

    async def find_similar_groups(self, max_per_group: int = 10) -> List[Dict]:
        """
        Находит похожие группы для уже найденных групп.
        Использует channels.GetChannelRecommendations API.

        Returns:
            Список новых найденных групп
        """
        new_groups = []
        seen_ids = set()

        # Получаем уже найденные группы из БД
        existing_groups = self.get_groups_from_db("discovered") + self.get_groups_from_db("joined")

        for group in existing_groups[:5]:  # Берём первые 5 групп для поиска похожих
            try:
                username = group.get("username")
                if not username:
                    continue

                logger.info(f"🔎 Searching similar groups for @{username}...")

                # Получаем InputChannel
                try:
                    channel = await self.client.resolve_peer(username)
                except Exception as e:
                    logger.debug(f"Cannot resolve @{username}: {e}")
                    continue

                # Запрашиваем рекомендации
                try:
                    result = await self.client.invoke(
                        functions.channels.GetChannelRecommendations(channel=channel)
                    )
                except Exception as e:
                    logger.debug(f"No recommendations for @{username}: {e}")
                    continue

                # Обрабатываем результат
                for chat in getattr(result, 'chats', []):
                    try:
                        if not isinstance(chat, types.Channel):
                            continue
                        if chat.broadcast:  # каналы пропускаем
                            continue

                        chat_id = chat.id
                        if chat_id in seen_ids:
                            continue
                        seen_ids.add(chat_id)

                        chat_username = getattr(chat, 'username', None)
                        if not chat_username:
                            continue

                        telegram_id = int(f"-100{chat.id}")

                        group_info = {
                            "telegram_group_id": telegram_id,
                            "title": getattr(chat, 'title', 'Unknown'),
                            "username": chat_username,
                            "description": "",
                            "members_count": getattr(chat, 'participants_count', 0) or 0,
                            "is_megagroup": getattr(chat, 'megagroup', False),
                            "is_public": True,
                        }

                        new_groups.append(group_info)
                        logger.debug(f"  Similar found: {group_info['title']}")

                    except Exception as e:
                        logger.debug(f"Error processing similar chat: {e}")

            except FloodWait as e:
                logger.warning(f"FloodWait: waiting {e.value}s")
                import asyncio
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.debug(f"Error in find_similar_groups for {group.get('title')}: {e}")

        logger.info(f"🔍 Found {len(new_groups)} similar groups")
        return new_groups

    async def get_group_full_info(self, username: str) -> Optional[Dict]:
        """Получает полную информацию о группе по username."""
        try:
            chat = await self.client.get_chat(username)

            return {
                "telegram_group_id": chat.id,
                "title": chat.title,
                "username": chat.username,
                "description": getattr(chat, "description", "") or "",
                "members_count": getattr(chat, "members_count", 0) or 0,
            }
        except Exception as e:
            logger.error(f"Error getting full info for {username}: {e}")
            return None

    def get_search_stats(self) -> Dict:
        """Возвращает статистику поиска."""
        return {
            "keywords_searched": len(self.searched_keywords),
            "keywords": self.searched_keywords
        }
