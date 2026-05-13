import asyncio
import logging
import sys
from typing import Optional
from pyrogram import Client, filters
from pyrogram.errors import RPCError as PyrogramException
from pyrogram.handlers import MessageHandler

# Импорт наших модулей
from config_agent import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TARGET_KEYWORDS,
    LOG_LEVEL, DB_PATH, PRIORITY_GROUPS
)
from agent_database import AgentDatabase
from llm_analyzer import LLMAnalyzer
from anti_ban_module import AntiBanManager
from scouting_module import ScoutingModule
from listening_module import ListeningModule
from engagement_module import EngagementModule
from proactive_module import ProactiveModule

# Настройка логирования
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Понижаем уровень шумных логов pyrogram (FloodWait warnings)
logging.getLogger("pyrogram.session.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection.connection").setLevel(logging.ERROR)


class TelegramAgent:
    """Главный класс Telegram AI-агента."""

    def __init__(self):
        self.db = AgentDatabase(DB_PATH)
        self.llm = LLMAnalyzer()
        self.client: Optional[Client] = None
        self.ban_manager: Optional[AntiBanManager] = None
        self.scouting: Optional[ScoutingModule] = None
        self.listening: Optional[ListeningModule] = None
        self.engagement: Optional[EngagementModule] = None
        self.proactive: Optional[ProactiveModule] = None
        self.proactive_task: Optional[asyncio.Task] = None
        self.join_task: Optional[asyncio.Task] = None
        self.agent_id: Optional[int] = None
        self.my_user_id: Optional[int] = None  # Кешированный user_id
        self.my_username: Optional[str] = None  # Кешированный username

    async def initialize(self) -> bool:
        """Инициализирует агента и все модули."""
        try:
            logger.info("=" * 60)
            logger.info("Initializing Telegram AI Agent...")
            logger.info("=" * 60)

            # Проверяем LLM (не блокируем запуск даже если не работает)
            logger.info("Checking OpenRouter API...")
            if not self.llm.health_check():
                logger.warning("⚠️  OpenRouter временно недоступен (вероятно rate limit)")
                logger.warning("   Агент запустится и будет повторять попытки во время работы")
                logger.warning("   Поиск групп и мониторинг будут работать без LLM")

            # Инициализируем БД
            logger.info("Initializing database...")
            self.db.init_db()

            # Создаем или получаем аккаунт агента
            logger.info("Setting up agent account...")
            # Для демонстрации используем agent_id = 1
            account = self.db.get_agent_account(1)
            if not account:
                logger.info("Creating new agent account...")
                self.agent_id = self.db.add_agent_account(
                    phone_number="placeholder",
                    session_name="agent_session_1"
                )
            else:
                self.agent_id = account["id"]

            logger.info(f"Agent ID: {self.agent_id}")

            # Создаем Pyrogram клиент
            logger.info("Initializing Pyrogram client...")
            self.client = Client(
                name="agent_session_1",
                api_id=TELEGRAM_API_ID,
                api_hash=TELEGRAM_API_HASH,
                in_memory=True  # Используем in-memory сессию для безопасности
            )

            # Инициализируем модули
            logger.info("Initializing modules...")
            self.ban_manager = AntiBanManager(self.db)
            self.scouting = ScoutingModule(self.client, self.db, self.llm)
            self.listening = ListeningModule(self.client, self.db, self.llm, agent_id=self.agent_id)
            self.engagement = EngagementModule(self.client, self.db, self.llm, self.agent_id)
            self.proactive = ProactiveModule(self.client, self.db, self.llm, self.agent_id)

            logger.info("=" * 60)
            logger.info("✅ Agent initialized successfully!")
            logger.info("=" * 60)
            return True

        except Exception as e:
            logger.error(f"Initialization error: {e}", exc_info=True)
            return False

    async def start_client(self) -> bool:
        """Запускает Pyrogram клиент."""
        try:
            logger.info("Starting Pyrogram client...")

            if not self.client:
                logger.error("Client not initialized")
                return False

            await self.client.start()
            logger.info("✅ Pyrogram client started")

            # Получаем информацию о себе ОДИН РАЗ и кешируем
            me = await self.client.get_me()
            self.my_user_id = me.id
            self.my_username = me.username
            logger.info(f"Logged in as: {me.first_name} (ID: {me.id})")

            return True

        except PyrogramException as e:
            if "AUTH_KEY_INVALID" in str(e) or "SESSION_REVOKED" in str(e):
                logger.error("Authentication failed. Please login:")
                logger.error("1. Delete the session file")
                logger.error("2. Run the script again and authenticate")
            else:
                logger.error(f"Pyrogram error: {e}")
            return False

        except Exception as e:
            logger.error(f"Error starting client: {e}", exc_info=True)
            return False

    async def stop_client(self):
        """Останавливает Pyrogram клиент и все фоновые задачи."""
        try:
            # Останавливаем проактивный цикл
            if self.proactive_task and not self.proactive_task.done():
                self.proactive_task.cancel()
                try:
                    await self.proactive_task
                except asyncio.CancelledError:
                    pass
                logger.info("Proactive loop stopped")

            # Останавливаем цикл вступлений
            if self.join_task and not self.join_task.done():
                self.join_task.cancel()
                try:
                    await self.join_task
                except asyncio.CancelledError:
                    pass
                logger.info("Join cycle stopped")

            if self.client:
                await self.client.stop()
                logger.info("Pyrogram client stopped")
        except Exception as e:
            logger.error(f"Error stopping client: {e}")

    async def run_scouting_cycle(self) -> bool:
        """Запускает цикл поиска групп."""
        try:
            logger.info("=" * 60)
            logger.info("Starting scouting cycle...")
            logger.info("=" * 60)

            if not self.scouting:
                logger.error("Scouting module not initialized")
                return False

            # 1. Ищем группы по ключевым словам
            found_groups = await self.scouting.search_groups(TARGET_KEYWORDS, max_results=10)
            logger.info(f"📋 Search found {len(found_groups)} groups")

            # Добавляем в БД те, которые прошли фильтрацию
            added_count = 0
            for group in found_groups:
                if await self.scouting.filter_group(group):
                    if await self.scouting.add_group_to_db(group):
                        added_count += 1

            logger.info(f"✅ Added {added_count} groups from search")

            # 2. Ищем похожие группы (бесплатно от Telegram!)
            logger.info("🔎 Looking for similar groups...")
            similar_groups = await self.scouting.find_similar_groups()

            similar_added = 0
            for group in similar_groups:
                if await self.scouting.filter_group(group):
                    if await self.scouting.add_group_to_db(group):
                        similar_added += 1

            logger.info(f"✅ Added {similar_added} similar groups")
            logger.info(f"📊 Total added this cycle: {added_count + similar_added}")

            return True

        except Exception as e:
            logger.error(f"Error in scouting cycle: {e}", exc_info=True)
            return False

    async def run_listening_cycle(self) -> bool:
        """Запускает цикл мониторинга групп."""
        try:
            logger.info("=" * 60)
            logger.info("Starting listening cycle...")
            logger.info("=" * 60)

            if not self.listening:
                logger.error("Listening module not initialized")
                return False

            # Получаем группы для мониторинга (только те что в БД)
            db_groups = self.scouting.get_groups_from_db("discovered")
            logger.info(f"Found {len(db_groups)} groups in database to join")

            # Ограничиваем количество одновременных вступлений (антибан)
            from config_agent import MAX_GROUPS_TO_JOIN_PER_DAY
            groups_to_join = db_groups[:min(len(db_groups), MAX_GROUPS_TO_JOIN_PER_DAY)]

            groups = await self.listening.join_groups(groups_to_join)

            joined_count = sum(1 for v in groups.values() if v)
            logger.info(f"Joined {joined_count} groups")

            # Обновляем статус групп в БД на 'joined'
            for db_group in groups_to_join:
                if groups.get(db_group.get("id")):
                    await self.scouting.update_group_status(db_group["id"], "joined")

            # Проверяем статус
            status = self.listening.get_monitoring_status()
            logger.info(f"Monitoring status: {status}")

            return True

        except Exception as e:
            logger.error(f"Error in listening cycle: {e}", exc_info=True)
            return False

    async def register_message_handler(self):
        """Регистрирует обработчик входящих сообщений в группах."""
        try:
            logger.info("Registering message handler...")

            async def handle_incoming_message(client, message):
                """Обрабатывает каждое входящее сообщение в группе."""
                try:
                    # Игнорируем свои сообщения (используем кешированный ID)
                    if message.from_user and message.from_user.id == self.my_user_id:
                        return

                    # Принудительная конвертация Pyrogram Str -> обычный Python str
                    # (через encode/decode избавляемся от багов с UTF-16 surrogate pairs)
                    raw = message.text or message.caption or ""
                    try:
                        msg_text = raw.encode("utf-8", "ignore").decode("utf-8") if raw else ""
                    except Exception:
                        msg_text = ""

                    if not msg_text.strip():
                        return

                    # Только групповые чаты
                    chat_type = getattr(message.chat.type, "name", "")
                    if chat_type not in ("GROUP", "SUPERGROUP"):
                        return

                    # Безопасный preview для лога
                    raw_title = message.chat.title or "Unknown"
                    try:
                        chat_title = raw_title.encode("utf-8", "ignore").decode("utf-8")
                    except Exception:
                        chat_title = "Unknown"
                    preview = msg_text[:60]
                    logger.debug(f"📨 New message in {chat_title}: {preview}...")

                    # Анализируем сообщение
                    analysis = self.llm.analyze_message(msg_text)

                    if not analysis["interested"] and analysis["interest_level"] < 0.3:
                        return

                    logger.info(f"💡 Relevant message detected! Interest: {analysis['interest_level']:.2f}")
                    logger.info(f"   Group: {chat_title}")
                    logger.info(f"   Text: {preview}")

                    # Проверяем лимиты
                    if not self.ban_manager.can_send_message(self.agent_id):
                        logger.warning("Rate limit reached, skipping response")
                        return

                    # Получаем group_id из БД
                    db_groups = self.db.get_target_groups(status="joined", limit=1000)
                    group_db_id = None
                    for g in db_groups:
                        if g.get("telegram_group_id") == message.chat.id:
                            group_db_id = g["id"]
                            break

                    if not group_db_id:
                        # Добавляем группу в БД, если ещё нет
                        group_db_id = self.db.add_target_group(
                            telegram_group_id=message.chat.id,
                            title=message.chat.title or "Unknown",
                            username=getattr(message.chat, "username", None),
                            members_count=getattr(message.chat, "members_count", 0)
                        )

                    # Ждём случайную задержку (антибан)
                    delay = self.ban_manager.get_delay(self.agent_id)
                    logger.info(f"⏳ Waiting {delay:.0f}s before responding...")
                    await asyncio.sleep(delay)

                    # Генерируем и отправляем ответ (передаем уже очищенный текст)
                    sent = await self.engagement.handle_message(message, group_db_id, safe_text=msg_text)

                    if sent:
                        self.ban_manager.register_message_sent(self.agent_id)
                        logger.info(f"✅ Response sent to {chat_title}")

                except Exception as e:
                    logger.error(f"Error handling message: {e}", exc_info=True)

            # Регистрируем обработчик
            handler = MessageHandler(handle_incoming_message, filters.text & filters.group)
            self.client.add_handler(handler)

            logger.info("✅ Message handler registered successfully")
            return True

        except Exception as e:
            logger.error(f"Error registering handler: {e}", exc_info=True)
            return False

    async def check_and_respond_to_messages(self) -> bool:
        """Проверяет наличие сообщений и отвечает на них."""
        try:
            logger.info("Checking for new messages...")

            if not self.engagement:
                logger.error("Engagement module not initialized")
                return False
            # Это место для подключения @client.on_message() обработчиков

            logger.debug("Message handlers are listening...")
            return True

        except Exception as e:
            logger.error(f"Error checking messages: {e}", exc_info=True)
            return False

    async def process_pending_groups(self):
        """Обрабатывает очередь групп добавленных через дашборд."""
        try:
            pending = self.db.get_pending_groups()
            if not pending:
                return

            logger.info(f"\n📋 Processing {len(pending)} pending groups from dashboard...")

            for task in pending:
                pid = task["id"]
                username = task["username"]

                try:
                    logger.info(f"⭐ Adding queued group: @{username}")

                    # Получаем информацию
                    chat = await self.client.get_chat(username)

                    # Добавляем в БД
                    gid = self.db.add_target_group(
                        telegram_group_id=chat.id,
                        title=chat.title or f"@{username}",
                        username=username,
                        description=getattr(chat, "description", "") or "",
                        members_count=getattr(chat, "members_count", 0) or 0
                    )

                    # Пробуем войти
                    try:
                        await self.client.join_chat(username)
                        logger.info(f"   ✅ Joined: {chat.title}")
                        if gid > 0:
                            self.db.update_group_status(gid, "joined")
                        # Добавляем в мониторинг
                        if chat.id not in self.listening.monitored_groups:
                            self.listening.monitored_groups.append(chat.id)
                        self.db.mark_pending_group_done(pid, "joined")

                    except Exception as je:
                        err_str = str(je)
                        if "ALREADY" in err_str.upper():
                            logger.info(f"   ✅ Already in: {chat.title}")
                            if gid > 0:
                                self.db.update_group_status(gid, "joined")
                            if chat.id not in self.listening.monitored_groups:
                                self.listening.monitored_groups.append(chat.id)
                            self.db.mark_pending_group_done(pid, "joined")
                        else:
                            logger.warning(f"   ⚠️  Added to DB but couldn't join: {je}")
                            self.db.mark_pending_group_done(pid, "added_only", str(je))

                except Exception as e:
                    logger.error(f"   ❌ Failed to add @{username}: {e}")
                    self.db.mark_pending_group_done(pid, "error", str(e))

                # Небольшая пауза между группами
                await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Error processing pending groups: {e}", exc_info=True)

    async def run_periodic_join_cycle(self):
        """Периодически:
        1. Каждые 30 сек - проверяет очередь от дашборда
        2. Каждые 6 часов - вступает в новые discovered группы
        """
        from config_agent import MAX_GROUPS_TO_JOIN_PER_DAY

        last_full_cycle = 0  # время последнего полного цикла (6h)

        while True:
            try:
                # 1. Быстрая проверка очереди от дашборда (каждые 30 сек)
                await self.process_pending_groups()

                # 2. Раз в 6 часов - обрабатываем discovered группы
                import time
                now = time.time()
                if now - last_full_cycle > 6 * 3600:
                    logger.info("\n🔄 Periodic join cycle starting...")
                    discovered = self.db.get_target_groups(status="discovered", limit=MAX_GROUPS_TO_JOIN_PER_DAY)

                    if discovered:
                        logger.info(f"📋 Trying to join {len(discovered)} new discovered groups...")
                        results = await self.listening.join_groups(discovered)
                        joined = sum(1 for v in results.values() if v)
                        logger.info(f"✅ Joined {joined}/{len(discovered)} new groups")
                    else:
                        logger.info("No new discovered groups to join")

                    last_full_cycle = now

                # Ждём 30 секунд до следующей проверки очереди
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                logger.info("Join cycle cancelled")
                break
            except Exception as e:
                logger.error(f"Error in join cycle: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def run_main_loop(self, run_forever: bool = True) -> bool:
        """Запускает главный цикл агента.

        Args:
            run_forever: Если True - агент работает непрерывно (рекомендуется),
                         если False - выполнит один цикл и завершится
        """
        try:
            logger.info("\n" + "=" * 60)
            logger.info("🚀 STARTING MAIN AGENT LOOP")
            logger.info("=" * 60 + "\n")

            # 1. Регистрируем обработчик входящих сообщений (один раз)
            await self.register_message_handler()

            # 1.5. Добавляем приоритетные группы (свои каналы, например @moneymaker_quest)
            if PRIORITY_GROUPS:
                logger.info("\n⭐ Adding priority groups...")
                await self.scouting.add_priority_groups(PRIORITY_GROUPS)

                # Сразу вступаем в приоритетные
                priority_db = []
                for username in PRIORITY_GROUPS:
                    clean = username.lstrip("@")
                    all_groups = self.db.get_target_groups(status="discovered", limit=1000)
                    for g in all_groups:
                        if g.get("username") == clean:
                            priority_db.append(g)
                            break

                if priority_db:
                    logger.info(f"⭐ Joining {len(priority_db)} priority groups immediately...")
                    await self.listening.join_groups(priority_db)

            # 2. Поиск групп
            await self.run_scouting_cycle()

            # 3. Вступление в группы
            await self.run_listening_cycle()

            # 4. Запускаем проактивный модуль в фоне (если включен)
            if self.proactive:
                logger.info("🚀 Starting proactive posting loop in background...")
                self.proactive_task = asyncio.create_task(
                    self.proactive.run_proactive_loop()
                )

            # 4.5. Запускаем периодический цикл вступлений в фоне
            self.join_task = asyncio.create_task(self.run_periodic_join_cycle())

            # 5. Запускаем мониторинг (бесконечный цикл)
            if run_forever:
                logger.info("\n" + "=" * 60)
                logger.info("👂 АГЕНТ В РЕЖИМЕ МОНИТОРИНГА + ПРОАКТИВНЫЕ ПОСТЫ")
                logger.info("   Ctrl+C для остановки")
                logger.info("=" * 60 + "\n")

                # Каждые 30 минут заново ищем группы и обновляем статус
                cycle_count = 0
                while True:
                    cycle_count += 1
                    await asyncio.sleep(1800)  # 30 минут

                    logger.info(f"\n--- Periodic Update {cycle_count} ---")
                    logger.info(f"Agent status: {self.ban_manager.get_agent_status(self.agent_id)}")

                    # Каждые 2 часа ищем новые группы
                    if cycle_count % 4 == 0:
                        await self.run_scouting_cycle()
                        await self.run_listening_cycle()

            return True

        except KeyboardInterrupt:
            logger.info("\n\n🛑 Stopped by user (Ctrl+C)")
            return True

        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            return False

    async def print_statistics(self):
        """Выводит статистику работы агента."""
        try:
            logger.info("\n" + "=" * 60)
            logger.info("📊 AGENT STATISTICS")
            logger.info("=" * 60)

            # Статистика БД
            groups = self.scouting.get_groups_from_db("discovered") if self.scouting else []
            logger.info(f"Groups in database: {len(groups)}")

            # Статистика мониторинга
            if self.listening:
                monitoring = self.listening.get_monitoring_status()
                logger.info(f"Groups monitored: {monitoring['groups_monitored']}")

            # Статистика взаимодействий
            if self.engagement:
                stats = self.engagement.get_engagement_stats()
                if stats:
                    logger.info(f"Total interactions: {stats.get('total_interactions', 0)}")
                    logger.info(f"Successful: {stats.get('successful', 0)}")

            # Статистика агента
            if self.ban_manager:
                agent_status = self.ban_manager.get_agent_status(self.agent_id)
                logger.info(f"Agent status: {agent_status}")

            logger.info("=" * 60 + "\n")

        except Exception as e:
            logger.error(f"Error printing statistics: {e}")

    async def run(self, run_forever: bool = True):
        """Главный метод запуска агента."""
        try:
            # Инициализация
            if not await self.initialize():
                logger.error("Failed to initialize agent")
                return False

            # Запуск клиента
            if not await self.start_client():
                logger.error("Failed to start client")
                return False

            try:
                # Главный цикл
                await self.run_main_loop(run_forever=run_forever)

            finally:
                # Статистика
                await self.print_statistics()

                # Остановка клиента
                await self.stop_client()

            return True

        except KeyboardInterrupt:
            logger.info("\nAgent stopped by user")
            await self.stop_client()
            return False

        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            return False


async def main():
    """Точка входа."""
    logger.info("Telegram AI Agent starting...")

    agent = TelegramAgent()
    success = await agent.run(run_forever=True)

    if success:
        logger.info("✅ Agent run completed successfully!")
        sys.exit(0)
    else:
        logger.error("❌ Agent run failed!")
        sys.exit(1)


if __name__ == "__main__":
    # Проверяем требования
    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 1234567:
        print("❌ Error: TELEGRAM_API_ID not configured!")
        print("Please set it in .env file (get it from https://my.telegram.org/apps)")
        sys.exit(1)

    if not TELEGRAM_API_HASH or TELEGRAM_API_HASH == "YOUR_API_HASH":
        print("❌ Error: TELEGRAM_API_HASH not configured!")
        print("Please set it in .env file (get it from https://my.telegram.org/apps)")
        sys.exit(1)

    # Запускаем агент
    asyncio.run(main())
