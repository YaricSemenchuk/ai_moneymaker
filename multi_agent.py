"""
🤖🤖🤖 Multi-Agent — запускает несколько Telegram аккаунтов параллельно.

Каждый агент:
- Имеет свой session-файл в sessions/
- Свои лимиты антибана
- Свои группы для мониторинга и постинга
- Работает независимо в своём asyncio task
"""
import asyncio
import logging
import os
import sys
import sqlite3
from typing import List, Dict, Optional
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import RPCError as PyrogramException

from config_agent import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TARGET_KEYWORDS,
    LOG_LEVEL, DB_PATH, PRIORITY_GROUPS,
    LISTENING_INTEREST_THRESHOLD,
    REACTIONS_ENABLED, REACTION_EMOJIS, REACTION_INTEREST_MIN,
    REACTION_INTEREST_MAX, REACTIONS_MAX_PER_HOUR,
    SESSIONS_DIR, GROUP_TITLE_BLACKLIST,
)
import random
import time as _time
from agent_database import AgentDatabase
from llm_analyzer import LLMAnalyzer
from anti_ban_module import AntiBanManager
from scouting_module import ScoutingModule
from listening_module import ListeningModule
from engagement_module import EngagementModule
from proactive_module import ProactiveModule

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection.connection").setLevel(logging.ERROR)


def _parse_proxy_url(url: Optional[str]) -> Optional[Dict]:
    """Парсит строку вида socks5://user:pass@host:port в dict для pyrogram."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        scheme = (p.scheme or "socks5").lower()
        if scheme not in ("socks5", "socks4", "http"):
            logger.warning(f"Unsupported proxy scheme: {scheme}")
            return None
        d = {"scheme": scheme, "hostname": p.hostname, "port": p.port or 1080}
        if p.username:
            d["username"] = p.username
        if p.password:
            d["password"] = p.password
        return d
    except Exception as e:
        logger.warning(f"Bad proxy_url '{url}': {e}")
        return None


class SingleAgent:
    """Один агент = один Telegram аккаунт."""

    def __init__(self, agent_id: int, phone: str, session_name: str, shared_db: AgentDatabase, shared_llm: LLMAnalyzer):
        self.agent_id = agent_id
        self.phone = phone
        self.session_name = session_name
        self.db = shared_db

        # Per-agent LLM с собственным referral_target
        from config_agent import DEFAULT_AGENT_REFERRALS, REFERRAL_BOT
        referral = self.db.get_agent_referral(agent_id) or DEFAULT_AGENT_REFERRALS.get(agent_id, REFERRAL_BOT)
        self.llm = LLMAnalyzer(referral_target=referral)
        # Подхватываем уже работающую модель из shared LLM (избегаем повторного health check)
        self.llm.model = shared_llm.model
        self.llm.models_to_try = list(shared_llm.models_to_try)
        self.referral_target = referral

        self.client: Optional[Client] = None
        self.ban_manager: Optional[AntiBanManager] = None
        self.scouting: Optional[ScoutingModule] = None
        self.listening: Optional[ListeningModule] = None
        self.engagement: Optional[EngagementModule] = None
        self.proactive: Optional[ProactiveModule] = None
        self.my_user_id: Optional[int] = None
        self.tasks: List[asyncio.Task] = []
        # Реакции: антифлуд (метки времени последних реакций за последний час).
        self._reaction_timestamps: List[float] = []
        # Кэш assigned-group_ids агента (TTL 60s) — для фильтра listening/replies
        self._assigned_group_ids_cache: Optional[set] = None
        self._assigned_cache_ts: float = 0.0

    def _can_react(self) -> bool:
        """Не превышен ли часовой лимит реакций."""
        now = _time.time()
        self._reaction_timestamps = [t for t in self._reaction_timestamps if now - t < 3600]
        return len(self._reaction_timestamps) < REACTIONS_MAX_PER_HOUR

    async def _try_react(self, message) -> bool:
        """Ставит реакцию на сообщение активного юзера. Тихо игнорит ошибки."""
        if not REACTIONS_ENABLED:
            return False
        try:
            if not self.db.get_agent_reactions_enabled(self.agent_id):
                return False
        except Exception:
            pass
        if not self._can_react():
            return False
        try:
            emoji = random.choice(REACTION_EMOJIS)
            await self.client.send_reaction(
                chat_id=message.chat.id,
                message_id=message.id,
                emoji=emoji,
            )
            self._reaction_timestamps.append(_time.time())
            logger.info(f"{self.log_prefix} {emoji} reacted in {message.chat.title or message.chat.id}")
            return True
        except Exception as e:
            logger.debug(f"{self.log_prefix} reaction failed: {e}")
            return False

    @property
    def log_prefix(self) -> str:
        return f"[Agent#{self.agent_id} {self.phone}]"

    async def start(self):
        """Запускает агента."""
        logger.info(f"{self.log_prefix} 🚀 Starting...")

        # Per-account: proxy + device fingerprint (важно для антибана)
        agent_info = self.db.get_agent_account(self.agent_id) or {}
        client_kwargs = dict(
            name=self.session_name,
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            workdir=SESSIONS_DIR,
        )
        proxy_dict = _parse_proxy_url(agent_info.get("proxy_url"))
        if proxy_dict:
            client_kwargs["proxy"] = proxy_dict
            logger.info(f"{self.log_prefix} 🌐 Using proxy {proxy_dict['hostname']}:{proxy_dict['port']}")
        for k, src in (("device_model", "device_model"),
                       ("system_version", "system_version"),
                       ("app_version", "app_version")):
            v = agent_info.get(src)
            if v:
                client_kwargs[k] = v

        self.client = Client(**client_kwargs)

        try:
            await self.client.start()
            me = await self.client.get_me()
            self.my_user_id = me.id
            logger.info(f"{self.log_prefix} ✅ Logged in as {me.first_name} (id={me.id})")
            gender = self.llm.apply_persona(me.first_name)
            gender_emoji = {'female': '👩', 'male': '👨', 'unknown': '🧑'}[gender]
            logger.info(f"{self.log_prefix} {gender_emoji} Persona: {me.first_name} → {gender}")
            logger.info(f"{self.log_prefix} 🎯 Promoting: {self.referral_target} (type={self.llm.target_type})")

            # Создаём модули
            self.ban_manager = AntiBanManager(self.db)
            self.scouting = ScoutingModule(self.client, self.db, self.llm)
            self.listening = ListeningModule(self.client, self.db, self.llm, agent_id=self.agent_id)
            self.engagement = EngagementModule(self.client, self.db, self.llm, self.agent_id)
            self.proactive = ProactiveModule(self.client, self.db, self.llm, self.agent_id)

            # Регистрируем обработчик сообщений
            self._register_handler()

            # Помечаем агента активным
            self.db.update_agent_status(self.agent_id, "active")

            # Стартовый цикл В ФОНЕ - не блокирует запуск других агентов
            self.tasks.append(asyncio.create_task(self._initial_setup()))

            return True

        except Exception as e:
            logger.error(f"{self.log_prefix} ❌ Failed to start: {e}", exc_info=True)
            return False

    async def stop(self):
        """Останавливает агента и фоновые задачи."""
        logger.info(f"{self.log_prefix} 🛑 Stopping...")

        # Отменяем фоновые задачи
        for task in self.tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Останавливаем клиент
        if self.client:
            try:
                await self.client.stop()
            except Exception as e:
                logger.warning(f"{self.log_prefix} Stop error: {e}")

        self.db.update_agent_status(self.agent_id, "inactive")
        logger.info(f"{self.log_prefix} ✅ Stopped")

    def _register_handler(self):
        """Регистрирует обработчик входящих сообщений."""
        async def handle_msg(client, message):
            try:
                # Игнорируем свои сообщения
                if message.from_user and message.from_user.id == self.my_user_id:
                    return

                # Конвертация Pyrogram Str -> обычный str
                raw = message.text or message.caption or ""
                try:
                    msg_text = raw.encode("utf-8", "ignore").decode("utf-8") if raw else ""
                except Exception:
                    msg_text = ""

                if not msg_text.strip():
                    return

                chat_type = getattr(message.chat.type, "name", "")
                if chat_type not in ("GROUP", "SUPERGROUP"):
                    return

                # Изоляция по назначению: если агенту назначены группы (например, по категории),
                # реагируем ТОЛЬКО на сообщения из них. Иначе работаем во всём общем пуле.
                try:
                    assigned_ids = self._assigned_group_ids_cache
                    if assigned_ids is None or _time.time() - self._assigned_cache_ts > 60:
                        assigned_ids = set(self.db.get_assigned_group_ids(self.agent_id))
                        self._assigned_group_ids_cache = assigned_ids
                        self._assigned_cache_ts = _time.time()
                    if assigned_ids:
                        # резолвим telegram_group_id → group_db_id
                        db_groups = self.db.get_groups_by_statuses(["joined", "active"], limit=2000)
                        my_chat_gid = next((g["id"] for g in db_groups
                                            if g.get("telegram_group_id") == message.chat.id), None)
                        if my_chat_gid is None or my_chat_gid not in assigned_ids:
                            logger.debug(f"{self.log_prefix} skip msg from non-assigned chat {message.chat.id}")
                            return
                except Exception as e:
                    logger.debug(f"assignment check error: {e}")

                # Безопасный заголовок
                try:
                    chat_title = (message.chat.title or "").encode("utf-8", "ignore").decode("utf-8") or "Unknown"
                except Exception:
                    chat_title = "Unknown"

                # Группа в чёрном списке — отвечать там нельзя. Отсекаем СРАЗУ,
                # до LLM-анализа и до анти-бан задержки: иначе агент впустую
                # жжёт 20-90с ожидания на каждое сообщение чатовой блэклист-группы.
                if any(b in chat_title.lower() for b in GROUP_TITLE_BLACKLIST):
                    return

                # Анализируем
                analysis = self.llm.analyze_message(msg_text)

                # Лёгкий порог для профайлинга (0.15) - собираем активных юзеров
                # Жёсткий порог для ответа (0.3) - чтобы не отвечать всем подряд
                interest = analysis["interest_level"]

                # Понижен порог профайлинга — собираем больше контекста
                if interest < 0.10:
                    return

                # === ПРОФАЙЛИНГ ЮЗЕРА ===
                # Сохраняем активных пользователей для аналитики
                user_profile_id = None
                if message.from_user:
                    try:
                        user_profile_id = self.db.upsert_user_profile(
                            telegram_user_id=message.from_user.id,
                            username=message.from_user.username,
                            first_name=message.from_user.first_name,
                            last_name=message.from_user.last_name,
                            language=analysis.get("language"),
                        )
                    except Exception as e:
                        logger.debug(f"Profile upsert error: {e}")

                # Если интерес низкий — только профайлим, не отвечаем
                if not analysis["interested"] and interest < LISTENING_INTEREST_THRESHOLD:
                    # Сохраняем сообщение в историю юзера (без ответа)
                    if user_profile_id:
                        try:
                            # Найдём group_db_id
                            tmp_groups = self.db.get_groups_by_statuses(["joined", "active"], limit=1000)
                            tmp_gid = next((g["id"] for g in tmp_groups if g.get("telegram_group_id") == message.chat.id), None)
                            if tmp_gid:
                                self.db.add_user_message(
                                    user_profile_id=user_profile_id,
                                    group_id=tmp_gid,
                                    message_text=msg_text[:500],
                                    interest_level=interest,
                                    intent=analysis.get("intent"),
                                    language=analysis.get("language"),
                                )
                        except Exception as e:
                            logger.debug(f"User message save error: {e}")
                    # Мягкое вовлечение: реакция в "тёплой зоне" interest.
                    if REACTION_INTEREST_MIN <= interest < REACTION_INTEREST_MAX:
                        await self._try_react(message)
                    return

                # Защита: если агент в зоне риска (3+ бана/failed за 24ч)
                # — отвечаем только на ОЧЕНЬ интересные сообщения (interest > 0.5)
                if self.ban_manager.is_agent_at_risk(self.agent_id) and interest < 0.5:
                    logger.debug(f"{self.log_prefix} at-risk skip (interest={interest:.2f})")
                    return

                # Антибан
                if not self.ban_manager.can_send_message(self.agent_id):
                    logger.warning(f"{self.log_prefix} Rate limit, skip")
                    return

                # Глобальный дневной бюджет действий — против поведенческих банов.
                if not self.ban_manager.has_daily_action_budget(self.agent_id):
                    return

                logger.info(f"{self.log_prefix} 💡 Relevant in {chat_title} (interest={interest:.2f})")

                # Получаем group_id из БД (joined или active)
                db_groups = self.db.get_groups_by_statuses(["joined", "active"], limit=1000)
                group_db_id = None
                for g in db_groups:
                    if g.get("telegram_group_id") == message.chat.id:
                        group_db_id = g["id"]
                        break

                if not group_db_id:
                    group_db_id = self.db.add_target_group(
                        telegram_group_id=message.chat.id,
                        title=chat_title,
                        username=getattr(message.chat, "username", None),
                        members_count=getattr(message.chat, "members_count", 0)
                    )

                # Случайная задержка
                delay = self.ban_manager.get_delay(self.agent_id)
                logger.info(f"{self.log_prefix} ⏳ Wait {delay:.0f}s before reply")
                await asyncio.sleep(delay)

                # Отвечаем
                sent = await self.engagement.handle_message(message, group_db_id, safe_text=msg_text)
                if sent:
                    self.ban_manager.register_message_sent(self.agent_id)
                    logger.info(f"{self.log_prefix} ✅ Replied in {chat_title}")

                # Сохраняем сообщение в историю юзера + отметка что ответили
                if user_profile_id:
                    try:
                        self.db.add_user_message(
                            user_profile_id=user_profile_id,
                            group_id=group_db_id,
                            message_text=msg_text[:500],
                            interest_level=interest,
                            intent=analysis.get("intent"),
                            language=analysis.get("language"),
                            replied_by_agent_id=self.agent_id if sent else None,
                        )
                        # Если успешно ответили — статус engaged
                        if sent:
                            self.db.update_user_status(user_profile_id, "engaged")
                    except Exception as e:
                        logger.debug(f"User message save error: {e}")

            except Exception as e:
                logger.error(f"{self.log_prefix} handle_msg error: {e}")

        handler = MessageHandler(handle_msg, filters.text & filters.group)
        self.client.add_handler(handler)
        logger.info(f"{self.log_prefix} ✅ Message handler registered")

    async def _leave_blacklisted_groups(self):
        """Выходит из групп, чьё название попадает в GROUP_TITLE_BLACKLIST.

        Агент там всё равно не отвечает (блэклист), а сидеть в группе =
        тратить циклы на анализ её сообщений. Запускается один раз при старте.
        Статус 'banned' включён в выборку, чтобы из группы вышли ВСЕ агенты,
        даже после того как первый пометил её глобально.
        """
        try:
            groups = self.db.get_groups_by_statuses(["joined", "active", "banned"], limit=2000)
        except Exception as e:
            logger.debug(f"{self.log_prefix} leave-blacklist: db error {e}")
            return
        left = 0
        for g in groups:
            title = (g.get("title") or "").lower()
            if not any(b in title for b in GROUP_TITLE_BLACKLIST):
                continue
            try:
                await self.client.leave_chat(g["telegram_group_id"])
                left += 1
                logger.info(f"{self.log_prefix} 🚪➖ Left blacklisted group: {g.get('title')}")
            except Exception:
                pass  # не состоим в группе / уже вышли — нормально
            try:
                self.db.blacklist_group(g["id"], reason="title blacklist auto-leave")
            except Exception:
                pass
            await asyncio.sleep(random.uniform(2, 5))  # анти-FloodWait
        if left:
            logger.info(f"{self.log_prefix} 🧹 Вышел из {left} блэклист-групп")

    async def _initial_setup(self):
        """Первичная настройка: priority groups + поиск + вступление."""
        # Чистка: выходим из блэклист-групп до всего остального.
        await self._leave_blacklisted_groups()

        # Priority groups (только агент #1 их добавляет, чтобы не дублировать)
        if self.agent_id == 1 and PRIORITY_GROUPS:
            logger.info(f"{self.log_prefix} ⭐ Adding priority groups...")
            await self.scouting.add_priority_groups(PRIORITY_GROUPS)

        # Поиск групп (только агент #1, остальные используют общую БД)
        if self.agent_id == 1:
            logger.info(f"{self.log_prefix} 🔍 Initial scouting...")
            try:
                found = await self.scouting.search_groups(TARGET_KEYWORDS, max_results=10)
                added = 0
                for g in found:
                    if await self.scouting.filter_group(g):
                        if await self.scouting.add_group_to_db(g):
                            added += 1
                logger.info(f"{self.log_prefix} ✅ Added {added} new groups")
            except Exception as e:
                logger.error(f"{self.log_prefix} Scouting error: {e}")

        # Каждый агент при старте вступает в МАЛУЮ часть групп (избегаем FloodWait)
        # Постепенно набирает больше через periodic_join_cycle (раз в 6 часов)
        from config_agent import MAX_GROUPS_TO_JOIN_PER_DAY

        # На старте: только 5 групп (избегаем FloodWait)
        START_LIMIT = 5

        # Сначала — догоняем joined группы (других агентов)
        joined_groups = self.db.get_target_groups(status="joined", limit=200)
        if joined_groups and self.agent_id != 1:  # Только не-первый агент догоняет
            to_catchup = joined_groups[:START_LIMIT]
            logger.info(f"{self.log_prefix} 🚪 Catch-up: joining {len(to_catchup)} groups where other agents are...")
            await self.listening.join_groups(to_catchup)

        # Потом - своя партиция НОВЫХ discovered групп
        all_discovered = self.db.get_target_groups(status="discovered", limit=200)
        my_share = self._partition_groups(all_discovered)[:START_LIMIT]

        if my_share:
            logger.info(f"{self.log_prefix} 🚪 Joining {len(my_share)} new groups (my partition)...")
            await self.listening.join_groups(my_share)

        # Запускаем фоновые задачи
        self.tasks.append(asyncio.create_task(self.proactive.run_proactive_loop()))
        self.tasks.append(asyncio.create_task(self._periodic_join_cycle()))
        # Scout-цикл запускаем у всех — внутри он сам решает дежурит ли
        # сегодня этот агент (детерминированный random по дате).
        self.tasks.append(asyncio.create_task(self._periodic_scout_cycle()))
        self.tasks.append(asyncio.create_task(self._periodic_views_cycle()))

    async def _periodic_views_cycle(self):
        """Раз в 30-90 минут (рандом) делаем проход просмотров, если флаг включён."""
        from views_module import auto_views_pass
        # стартовая пауза чтобы агенты не стартовали одновременно
        await asyncio.sleep(random.randint(60, 600))
        while True:
            try:
                if not self.db.get_agent_views_enabled(self.agent_id):
                    await asyncio.sleep(600)
                    continue

                # joined-каналы агента
                memberships = self.db.get_agent_memberships(
                    self.agent_id, statuses=['joined', 'active']
                ) or []
                # подмешаем username/status в плоский dict для модуля
                groups = []
                for m in memberships:
                    groups.append({
                        'id': m.get('group_id') or m.get('id'),
                        'username': m.get('username'),
                        'status': m.get('status'),
                        'title': m.get('title'),
                    })
                groups = [g for g in groups if g.get('username')]

                if groups:
                    await auto_views_pass(
                        self.agent_id, self.client, self.db, groups,
                        log_prefix=self.log_prefix, max_groups=5,
                    )
            except Exception as e:
                logger.warning(f"{self.log_prefix} views cycle error: {e}")
            # 30-90 минут до следующего прогона
            await asyncio.sleep(random.randint(1800, 5400))

    def _partition_groups(self, groups: List[Dict]) -> List[Dict]:
        """
        Распределяет ТОЛЬКО НОВЫЕ discovered группы между агентами.
        Это для join-цикла - чтобы агенты не дублировали попытки вступления.

        ВАЖНО: после вступления каждый агент может отвечать в ЛЮБОЙ joined группе
        (где он реально находится). Это даёт максимум шансов на ответ.
        """
        # Получаем активных агентов
        conn = sqlite3.connect(self.db.db_path)
        cur = conn.cursor()
        cur.execute("SELECT id FROM agent_accounts WHERE status = 'active' ORDER BY id")
        active_ids = [row[0] for row in cur.fetchall()]
        conn.close()

        if not active_ids or len(active_ids) == 1:
            return groups

        # Агенты с id [1, 2, 5] → индексы [0, 1, 2]
        try:
            my_index = active_ids.index(self.agent_id)
        except ValueError:
            my_index = 0

        n_agents = len(active_ids)
        return [g for g in groups if g["id"] % n_agents == my_index]

    async def _periodic_join_cycle(self):
        """
        Быстрый цикл (каждые 20 сек):
        - Проверяет очередь pending_groups (от дашборда)
        - Проверяет очередь pending_searches (парсинг по словам)

        Медленный цикл (раз в 6 часов):
        - Вступает в новые discovered группы
        """
        import time
        from config_agent import MAX_GROUPS_TO_JOIN_PER_DAY

        last_join_time = 0  # время последнего join cycle

        while True:
            try:
                # БЫСТРАЯ часть - каждый цикл (~20 сек)
                await self._process_pending_groups()  # включает и поиски

                # МЕДЛЕННАЯ часть - раз в 6 часов
                now = time.time()
                if now - last_join_time > 6 * 3600:
                    last_join_time = now

                    logger.info(f"{self.log_prefix} 🔄 Periodic join cycle (every 6h)...")
                    discovered = self.db.get_target_groups(status="discovered", limit=200)
                    my_share = self._partition_groups(discovered)[:MAX_GROUPS_TO_JOIN_PER_DAY]

                    if my_share:
                        logger.info(f"{self.log_prefix} 🚪 Joining {len(my_share)} groups (my partition)")
                        await self.listening.join_groups(my_share)

                # Короткая пауза до следующей проверки очереди
                await asyncio.sleep(20)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{self.log_prefix} Join cycle error: {e}")
                await asyncio.sleep(60)

    async def _process_pending_groups(self):
        """Обрабатывает очередь от дашборда (только агент #1)."""
        if self.agent_id != 1:
            return

        try:
            pending = self.db.get_pending_groups()
            if pending:
                logger.info(f"{self.log_prefix} 📋 Processing {len(pending)} pending groups")

                for task in pending:
                    pid = task["id"]
                    username = task["username"]
                    try:
                        chat = await self.client.get_chat(username)
                        gid = self.db.add_target_group(
                            telegram_group_id=chat.id,
                            title=chat.title or f"@{username}",
                            username=username,
                            description=getattr(chat, "description", "") or "",
                            members_count=getattr(chat, "members_count", 0) or 0
                        )

                        try:
                            await self.client.join_chat(username)
                            if gid > 0:
                                self.db.update_group_status(gid, "joined")
                            self.db.mark_pending_group_done(pid, "joined")
                        except Exception as je:
                            if "ALREADY" in str(je).upper():
                                if gid > 0:
                                    self.db.update_group_status(gid, "joined")
                                self.db.mark_pending_group_done(pid, "joined")
                            else:
                                self.db.mark_pending_group_done(pid, "added_only", str(je))
                    except Exception as e:
                        self.db.mark_pending_group_done(pid, "error", str(e))

                    await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"{self.log_prefix} Pending groups error: {e}")

        # Также обрабатываем очередь поисков
        await self._process_pending_searches()

    async def _process_pending_searches(self):
        """Обрабатывает очередь поисков по ключевым словам (только агент #1)."""
        if self.agent_id != 1:
            return

        try:
            pending = self.db.get_pending_searches()
            if not pending:
                return

            logger.info(f"{self.log_prefix} 🔎 Processing {len(pending)} keyword search requests")

            for task in pending:
                sid = task["id"]
                keywords_str = task["keywords"]
                max_results = task["max_results"]
                src_category = task.get("source_category")

                # Парсим ключевые слова
                keywords = [k.strip() for k in keywords_str.split(",") if k.strip()]

                logger.info(f"{self.log_prefix}   🔍 Search request #{sid}: {len(keywords)} keywords (max={max_results}, cat={src_category})")
                logger.info(f"{self.log_prefix}   Keywords: {keywords[:5]}{'...' if len(keywords) > 5 else ''}")

                try:
                    found = await self.scouting.search_groups(keywords, max_results=max_results)

                    added = 0
                    for g in found:
                        if src_category and not g.get("matched_category"):
                            g["matched_category"] = src_category
                        if await self.scouting.filter_group(g):
                            if await self.scouting.add_group_to_db(g):
                                added += 1

                    self.db.mark_search_done(sid, "done", found=len(found), added=added)
                    logger.info(f"{self.log_prefix}   ✅ Search #{sid} done: found={len(found)}, added={added}")

                except Exception as e:
                    logger.error(f"{self.log_prefix}   ❌ Search #{sid} failed: {e}")
                    self.db.mark_search_done(sid, "error", error=str(e))

                # Пауза между поисками
                await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"{self.log_prefix} Pending searches error: {e}")

    def _is_scout_duty_today(self) -> bool:
        """Дежурит ли этот агент сегодня (детерминированный random по дате).

        Все агенты независимо получают одинаковый набор из
        AUTO_SCOUT_AGENTS_PER_DAY случайных id среди активных, потому что
        seed = сегодняшняя дата. Так не нужна координация.
        """
        try:
            import random as _r
            from datetime import datetime as _dt
            from config_agent import AUTO_SCOUT_AGENTS_PER_DAY
            conn = self.db.get_connection()
            rows = conn.execute(
                "SELECT id FROM agent_accounts "
                "WHERE phone_number != 'placeholder' AND status != 'banned' "
                "ORDER BY id"
            ).fetchall()
            conn.close()
            ids = [r[0] for r in rows]
            if not ids:
                return False
            if self.agent_id not in ids:
                return False
            rnd = _r.Random(f"scout-duty-{_dt.now().date().isoformat()}")
            duty = set(rnd.sample(ids, min(AUTO_SCOUT_AGENTS_PER_DAY, len(ids))))
            return self.agent_id in duty
        except Exception as e:
            logger.debug(f"{self.log_prefix} scout-duty check failed: {e}")
            return False

    async def _periodic_scout_cycle(self):
        """
        Smart daily scouting (дежурят AUTO_SCOUT_AGENTS_PER_DAY случайных
        агентов в день — детерминированно по дате):
        - 2 раза в день в случайное время (около 11 и 18 по Киеву)
        - 6 случайных слов из большого пула на каждый прогон
        - 30-60 сек между словами
        - Стоп при FloodWait — до завтра
        """
        import random
        from datetime import datetime, timedelta
        from config_agent import (
            AUTO_SCOUT_ENABLED, AUTO_SCOUT_TIMES, AUTO_SCOUT_KEYWORDS_PER_RUN,
            AUTO_SCOUT_DELAY_MIN, AUTO_SCOUT_DELAY_MAX, AUTO_SCOUT_TIME_JITTER,
            SCOUT_KEYWORD_POOL
        )

        if not AUTO_SCOUT_ENABLED:
            logger.info(f"{self.log_prefix} Auto-scout disabled in config")
            return

        # Сидим БД из конфига если пусто
        self.db.seed_keyword_pool(SCOUT_KEYWORD_POOL)

        # Берём актуальный пул из БД (может быть отредактирован пользователем)
        all_keywords = [kw["keyword"] for kw in self.db.get_all_keywords(only_enabled=True)]

        logger.info(f"{self.log_prefix} 🔍 Auto-scout enabled: {len(AUTO_SCOUT_TIMES)}x/day, {AUTO_SCOUT_KEYWORDS_PER_RUN} kw/run from pool of {len(all_keywords)}")

        last_run_date = None  # чтобы не запускать дважды в один и тот же слот

        while True:
            try:
                now = datetime.now()
                current_hour = now.hour

                # Проверяем — пора ли запускать?
                should_run = False
                slot_key = None
                for hour in AUTO_SCOUT_TIMES:
                    # +- 30 минут jitter
                    if abs(current_hour - hour) <= 1:
                        slot_key = f"{now.date()}_{hour}"
                        if slot_key != last_run_date:
                            should_run = True
                            break

                if should_run:
                    last_run_date = slot_key

                    # Раньше дежурили только AUTO_SCOUT_AGENTS_PER_DAY агентов
                    # детерминированно по дате — это тормозило обновление пула групп.
                    # Теперь все агенты скаутят независимо каждый слот.
                    logger.info(f"{self.log_prefix} 🔍 Запускаю парсинг групп")

                    # Случайная задержка ±30 мин для непредсказуемости
                    jitter = random.randint(-AUTO_SCOUT_TIME_JITTER, AUTO_SCOUT_TIME_JITTER)
                    if jitter > 0:
                        logger.info(f"{self.log_prefix} ⏳ Random scout jitter: +{jitter}s ({jitter//60}min)")
                        await asyncio.sleep(jitter)

                    # Перечитываем актуальный пул из БД (вдруг пользователь редактировал)
                    fresh_keywords = [kw["keyword"] for kw in self.db.get_all_keywords(only_enabled=True)]
                    if not fresh_keywords:
                        logger.warning(f"{self.log_prefix} Keyword pool пуст, пропускаю прогон")
                        await asyncio.sleep(300)
                        continue

                    # Берём случайные слова
                    selected = random.sample(fresh_keywords, min(AUTO_SCOUT_KEYWORDS_PER_RUN, len(fresh_keywords)))
                    logger.info(f"{self.log_prefix} 🎯 Daily scout starting with {len(selected)} random keywords:")
                    for kw in selected:
                        logger.info(f"{self.log_prefix}    • {kw}")

                    # Логируем начало прогона
                    scout_run_id = None
                    try:
                        scout_run_id = self.db.start_scout_run(self.agent_id, selected)
                    except Exception as e:
                        logger.debug(f"start_scout_run failed: {e}")

                    flood_wait_hit = False
                    total_found = 0
                    total_added = 0

                    for i, keyword in enumerate(selected):
                        if flood_wait_hit:
                            logger.warning(f"{self.log_prefix} 🛑 FloodWait detected — stopping until tomorrow")
                            break

                        try:
                            logger.info(f"{self.log_prefix} 🔎 [{i+1}/{len(selected)}] Searching: '{keyword}'")
                            found = await self.scouting.search_groups([keyword], max_results=10)

                            for g in found:
                                if await self.scouting.filter_group(g):
                                    if await self.scouting.add_group_to_db(g):
                                        total_added += 1

                            total_found += len(found)

                            # Пауза между словами
                            if i < len(selected) - 1:
                                pause = random.uniform(AUTO_SCOUT_DELAY_MIN, AUTO_SCOUT_DELAY_MAX)
                                logger.info(f"{self.log_prefix}    💤 Pause {pause:.0f}s before next keyword")
                                await asyncio.sleep(pause)

                        except Exception as e:
                            err_str = str(e).upper()
                            if "FLOOD" in err_str:
                                logger.warning(f"{self.log_prefix} ⚠️ FloodWait in scout — stop")
                                flood_wait_hit = True
                            else:
                                logger.error(f"{self.log_prefix} Search error: {e}")

                    logger.info(f"{self.log_prefix} ✅ Daily scout done: found={total_found}, added={total_added}")

                    # Закрываем прогон
                    if scout_run_id is not None:
                        try:
                            self.db.finish_scout_run(
                                scout_run_id,
                                groups_found=total_found,
                                groups_added=total_added,
                                status='floodwait' if flood_wait_hit else 'done',
                            )
                        except Exception as e:
                            logger.debug(f"finish_scout_run failed: {e}")

                # Проверяем каждые 5 минут
                await asyncio.sleep(300)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{self.log_prefix} Scout cycle error: {e}")
                await asyncio.sleep(600)


class MultiAgent:
    """Менеджер нескольких агентов."""

    def __init__(self):
        self.db = AgentDatabase(DB_PATH)
        self.llm = LLMAnalyzer()
        self.agents: List[SingleAgent] = []

    def get_active_accounts(self) -> List[Dict]:
        """Получает все аккаунты из БД."""
        conn = sqlite3.connect(self.db.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, phone_number, session_name, status
            FROM agent_accounts
            WHERE status != 'banned'
            ORDER BY id
        """)
        rows = cur.fetchall()
        conn.close()

        return [{"id": r[0], "phone": r[1], "session_name": r[2], "status": r[3]} for r in rows]

    async def run(self):
        """Запускает все аккаунты параллельно."""
        # Проверка что нет других процессов держащих session-файлы
        import subprocess
        try:
            result = subprocess.run(
                ["pgrep", "-f", "python.*(main.py|multi_agent)"],
                capture_output=True, text=True
            )
            pids = [p for p in result.stdout.strip().split("\n") if p and int(p) != os.getpid()]
            if pids:
                logger.error(f"❌ Уже запущено {len(pids)} других процессов агента!")
                logger.error(f"   PIDs: {pids}")
                logger.error("   Останови их через: pkill -9 -f 'python.*multi_agent'")
                return
        except Exception:
            pass

        accounts = self.get_active_accounts()

        if not accounts:
            logger.error("❌ Нет аккаунтов в БД!")
            logger.error("   Запусти: python add_account.py")
            return

        logger.info("=" * 60)
        logger.info(f"🤖 MULTI-AGENT STARTING: {len(accounts)} accounts")
        logger.info("=" * 60)
        for acc in accounts:
            logger.info(f"   #{acc['id']}: {acc['phone']} ({acc['session_name']})")
        logger.info("=" * 60)

        # Health check LLM один раз
        logger.info("🔍 Checking OpenRouter API...")
        if not self.llm.health_check():
            logger.warning("⚠️  OpenRouter rate limited - will retry during work")

        # Создаём агентов
        for acc in accounts:
            agent = SingleAgent(
                agent_id=acc["id"],
                phone=acc["phone"],
                session_name=acc["session_name"],
                shared_db=self.db,
                shared_llm=self.llm
            )
            self.agents.append(agent)

        # Параллельно подключаем всех агентов (с небольшим разносом по 2 сек)
        async def start_with_delay(agent, idx):
            await asyncio.sleep(idx * 2)
            return await agent.start()

        logger.info("🔌 Starting all agents in parallel...")
        results = await asyncio.gather(
            *[start_with_delay(agent, i) for i, agent in enumerate(self.agents)],
            return_exceptions=True
        )

        # Проверяем кто запустился
        success_count = 0
        for agent, result in zip(self.agents, results):
            if isinstance(result, Exception):
                logger.error(f"❌ Agent #{agent.agent_id} crashed: {result}")
            elif result:
                success_count += 1
            else:
                logger.error(f"⚠️  Agent #{agent.agent_id} failed to start")

        logger.info("\n" + "=" * 60)
        logger.info(f"👂 RUNNING: {success_count}/{len(self.agents)} agents - Press Ctrl+C to stop")
        logger.info("=" * 60 + "\n")

        # Ждём бесконечно (агенты работают в своих фоновых задачах)
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("\n🛑 Stopping all agents...")
            for agent in self.agents:
                await agent.stop()


async def main():
    multi = MultiAgent()
    await multi.run()


if __name__ == "__main__":
    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 1234567:
        print("❌ TELEGRAM_API_ID not configured in .env")
        sys.exit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
