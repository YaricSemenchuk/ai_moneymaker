"""
🚀 Проактивный модуль - агент САМ инициирует диалоги в группах.
Безопасный режим с лимитами и рандомизацией.
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pyrogram import Client
from pyrogram.errors import RPCError as PyrogramException
from agent_database import AgentDatabase
from llm_analyzer import LLMAnalyzer
from admin_notifier import notify_ban
from config_agent import (
    REFERRAL_BOT,
    PROACTIVE_ENABLED,
    PROACTIVE_MAX_POSTS_PER_DAY,
    PROACTIVE_MIN_INTERVAL_HOURS,
    PROACTIVE_MAX_INTERVAL_HOURS,
    PROACTIVE_GROUP_COOLDOWN_DAYS,
    PROACTIVE_MAX_POSTS_PER_GROUP_PER_DAY,
    PROACTIVE_ACTIVE_HOUR_START,
    PROACTIVE_ACTIVE_HOUR_END,
    MIN_GROUP_MEMBERS,
    MAX_GROUP_MEMBERS_FOR_PROACTIVE,
    AUTO_DISABLE_AFTER_FAILS,
    GROUP_TITLE_BLACKLIST,
    SLOWMODE_BACKOFF_MULTIPLIER,
    SLOWMODE_MIN_COOLDOWN_SEC,
)

# Глобальный slow-mode кулдаун (между запусками агентов не теряется в рамках процесса)
# group_id → unix timestamp до которого не трогать
_slowmode_cooldowns: Dict[int, float] = {}

logger = logging.getLogger(__name__)


# Шаблоны промптов для генерации естественных постов.
# 3 категории × 4 варианта. Каждая категория ориентирована на разный психотриггер:
#   A — социальное доказательство (для group/канала)
#   B — конкретный кейс с цифрой (для bot)
#   C — любопытство/вопрос с подвохом (универсальный)
# Скучные цифры ($3-$15) звучат правдоподобно. Большие — триггерят "реклама".
POST_TEMPLATES_RU = [
    # === A. Социальное доказательство ===
    {"name": "A_сообщество_кейс",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение от лица обычного пользователя: ты состоишь в {bot}, на прошлой неделе там разобрали 2-3 рабочих способа подработки. Не реклама, просто упомянуть в тему разговора. Без эмодзи в начале, без хэштегов."},
    {"name": "A_активность",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: ты сидишь в {bot}, там за вчера человек 5 кидали свои выплаты — мелочь $3-$7, но реальные. Спроси кто ещё там состоит. Без рекламного тона."},
    {"name": "A_новость_оттуда",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение, будто пересказываешь что услышал в чате {bot}: там обсуждали свежие способы вывода USDT. Звучи как живой человек, ничего не продавай."},
    {"name": "A_подсказка_новичку",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение в ответ воображаемому новичку: если ищешь куда подсесть — посмотри {bot}, там нормальные мужики, не наёб. Никакого хайпа."},

    # === B. Конкретный кейс с цифрой (для bot) ===
    {"name": "B_кейс_небольшой",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: вчера сделал 4-5 заданий в {bot}, вышло $6 в USDT — не пятизначное, но на доставку хватает. Без восторгов, спокойно."},
    {"name": "B_кейс_недельный",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: за неделю накапал около $20 на {bot}, простые задания (реги, опросы, чек-ин). Спроси кто ещё ковыряется."},
    {"name": "B_кейс_быстрый",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: с утра завис на {bot}, минут 40 — вывел $3.50 в USDT. Подзаработать на кофе. Натурально, без emoji."},
    {"name": "B_кейс_осторожный",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: пробовал {bot} последний месяц, в среднем $5-12 в день если есть свободные пара часов. Без хайпа, просто факт."},

    # === C. Любопытство / вопрос с подвохом ===
    {"name": "C_подвох",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение от новичка: наткнулся на {bot}, задания платные в USDT. Кто-нибудь пробовал — есть подвох или норм? Без рекламы."},
    {"name": "C_уточнение",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: видел рекомендуют {bot}, но боюсь время потратить впустую. Может кто реально выводил оттуда?"},
    {"name": "C_сравнение",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: между {bot} и кучей других ботов с заданиями — кто реально платит? Спрашиваю у тех кто пробовал, не у админов."},
    {"name": "C_проверка",
     "prompt": "Сгенерируй короткое (1-2 предложения) сообщение: тут советовали {bot}, минимальный вывод вроде маленький. Кто проверял, реально выводится в USDT?"},
]

POST_TEMPLATES_EN = [
    {"name": "A_community_proof",
     "prompt": "Generate a short (1-2 sentence) message as a regular user: you're in {bot}, last week people there shared 2-3 actual side gigs. Not an ad, just casual mention. No emojis at start, no hashtags."},
    {"name": "A_activity",
     "prompt": "Generate a short (1-2 sentence) message: you saw in {bot} like 5 people posted their payouts yesterday — small $3-$7 but real. Ask who else is in there. No promo tone."},
    {"name": "B_small_case",
     "prompt": "Generate a short (1-2 sentence) message: did 4-5 tasks on {bot} yesterday, got $6 USDT. Not life-changing but ok. Casual, no hype."},
    {"name": "B_week_case",
     "prompt": "Generate a short (1-2 sentence) message: pulled about $20 from {bot} this week, simple tasks (signups, surveys). Ask who else is grinding it."},
    {"name": "C_skeptical",
     "prompt": "Generate a short (1-2 sentence) message from a newbie: stumbled on {bot}, USDT payouts. Anyone tried, is there a catch? Not promo."},
    {"name": "C_minwithdraw",
     "prompt": "Generate a short (1-2 sentence) message: people recommend {bot}, withdrawal minimum looks low. Anyone actually cashed out in USDT?"},
]


class ProactiveModule:
    """Модуль для проактивных постов в группах."""

    def __init__(self, client: Client, db: AgentDatabase, llm: LLMAnalyzer, agent_id: int):
        self.client = client
        self.db = db
        self.llm = llm
        self.agent_id = agent_id
        self.last_post_time: Optional[datetime] = None

    def is_active_hour(self) -> bool:
        """Проверяет, активное ли сейчас время (день)."""
        now_hour = datetime.now().hour
        return PROACTIVE_ACTIVE_HOUR_START <= now_hour < PROACTIVE_ACTIVE_HOUR_END

    def can_post_today(self) -> bool:
        """Проверяет лимит постов в день."""
        count = self.db.count_proactive_posts_today(self.agent_id)
        can = count < PROACTIVE_MAX_POSTS_PER_DAY
        if not can:
            logger.info(f"📊 Daily proactive post limit reached: {count}/{PROACTIVE_MAX_POSTS_PER_DAY}")
        return can

    def _was_banned_in(self, group_db_id: int) -> bool:
        """Получал ли любой агент USER_BANNED в этой группе."""
        try:
            conn = self.db.get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM agent_group_membership "
                "WHERE group_id=? AND status='banned' LIMIT 1",
                (group_db_id,),
            )
            r = cur.fetchone()
            conn.close()
            return r is not None
        except Exception:
            return False

    def can_post_to_group(self, group_id: int) -> bool:
        """Проверяет cooldown по группе И per-agent дневной лимит в эту группу."""
        # Жёсткий лимит: не более N постов от этого агента в эту группу за 24ч.
        posted_today = self.db.count_proactive_posts_today_in_group(self.agent_id, group_id)
        if posted_today >= PROACTIVE_MAX_POSTS_PER_GROUP_PER_DAY:
            logger.debug(
                f"Group {group_id}: agent {self.agent_id} already posted "
                f"{posted_today}/{PROACTIVE_MAX_POSTS_PER_GROUP_PER_DAY} times in 24h"
            )
            return False

        last_time = self.db.get_last_proactive_post_time(group_id)
        if not last_time:
            return True

        cooldown = timedelta(days=PROACTIVE_GROUP_COOLDOWN_DAYS)
        elapsed = datetime.now() - last_time
        can = elapsed > cooldown
        if not can:
            remaining = cooldown - elapsed
            logger.debug(f"Group {group_id} cooldown: {remaining.days}d remaining")
        return can

    def select_target_group(self) -> Optional[Dict]:
        """Выбирает случайную подходящую группу для поста."""
        # Группы где агент состоит (joined) ИЛИ уже активен (active)
        groups = self.db.get_groups_by_statuses(["joined", "active"], limit=200)

        # Назначенные этому агенту группы (если есть ≥1 — постим только в них).
        assigned_ids = set(self.db.get_assigned_group_ids(self.agent_id))
        if assigned_ids:
            before = len(groups)
            groups = [g for g in groups if g["id"] in assigned_ids]
            logger.debug(
                f"Agent {self.agent_id}: assignment filter "
                f"{before}→{len(groups)} ({len(assigned_ids)} assigned)"
            )

        # Исключаем каналы — туда обычные юзеры не могут писать
        groups = [g for g in groups if not g.get("is_channel")]

        # Фильтр размера: мелкие = шум, мегаканалы = пост утонет
        groups = [
            g for g in groups
            if (g.get("members_count") or 0) >= MIN_GROUP_MEMBERS
            and (g.get("members_count") or 0) <= MAX_GROUP_MEMBERS_FOR_PROACTIVE
        ]

        # Опасные ниши: пропускаем по названию (детские/игровые/строгие)
        def is_blacklisted(g):
            t = (g.get("title") or "").lower()
            return any(b in t for b in GROUP_TITLE_BLACKLIST)
        before_bl = len(groups)
        groups = [g for g in groups if not is_blacklisted(g)]
        if before_bl - len(groups):
            logger.debug(f"⛔ Пропустил {before_bl - len(groups)} blacklisted-групп")

        # Slow-mode кулдаун: если группа недавно вернула SLOWMODE_WAIT — ждём
        import time as _t
        now_ts = _t.time()
        groups = [g for g in groups if _slowmode_cooldowns.get(g["id"], 0) <= now_ts]

        # Авто-выкидываем группы с подряд-фейлами (стабильно режут наши посты)
        before = len(groups)
        groups = [
            g for g in groups
            if self.db.count_recent_consecutive_failures(g["id"]) < AUTO_DISABLE_AFTER_FAILS
        ]
        skipped = before - len(groups)
        if skipped:
            logger.info(f"⛔ Пропустил {skipped} групп с {AUTO_DISABLE_AFTER_FAILS}+ подряд фейлами")

        # Группы где хоть один агент уже получал USER_BANNED — НЕ постим вообще
        before_b = len(groups)
        groups = [g for g in groups if not self._was_banned_in(g["id"])]
        if before_b - len(groups):
            logger.info(f"⛔ Пропустил {before_b - len(groups)} групп где кто-то из агентов был забанен")

        if not groups:
            logger.debug("No joined groups available")
            return None

        # Фильтруем те, в которые можно писать (cooldown прошёл)
        eligible = [g for g in groups if self.can_post_to_group(g["id"])]

        if not eligible:
            logger.info(f"⏳ All {len(groups)} joined groups are in cooldown")
            return None

        # Приоритезация: больше шанс попасть в группу, где скопилось больше
        # необработанных тёплых new-лидов (avg_interest >= 0.4). Базовый вес 1,
        # плюс по 1 за каждого горячего лида — так пост точечно "дожимает".
        hot = self.db.count_hot_leads_by_group(min_interest=0.4)
        weights = [1 + hot.get(g["id"], 0) for g in eligible]
        chosen = random.choices(eligible, weights=weights, k=1)[0]
        logger.info(
            f"🎯 Selected group: {chosen['title']} (id={chosen['id']}, "
            f"hot_leads={hot.get(chosen['id'], 0)})"
        )
        return chosen

    async def generate_post(self, language: str = "ru") -> Optional[Dict]:
        """Генерирует естественный пост через LLM."""
        templates = POST_TEMPLATES_RU if language == "ru" else POST_TEMPLATES_EN
        template = random.choice(templates)

        # Используем target из LLM analyzer (per-agent)
        target = self.llm.referral_target
        target_type = self.llm.target_type  # "bot" или "group"

        # СНАЧАЛА подставляем target в плейсхолдер {bot}, потом заменяем слова
        # (порядок важен: иначе .replace("bot", "group") сломает плейсхолдер)
        prompt_text = template["prompt"].format(bot=target)

        # Если promotim группу/канал — переформулируем естественнее
        if target_type == "group":
            prompt_text = prompt_text.replace("бота", "группу")
            prompt_text = prompt_text.replace("бот ", "группу ")
            prompt_text = prompt_text.replace(" bot", " group")
        elif target_type == "channel":
            prompt_text = prompt_text.replace("бота", "канал")
            prompt_text = prompt_text.replace("бот ", "канал ")
            prompt_text = prompt_text.replace(" bot", " channel")

        messages = [
            {"role": "system", "content": self.llm.agent_prompt},
            {"role": "user", "content": prompt_text}
        ]

        post_text = self.llm._make_request(messages, temperature=0.9)

        if not post_text:
            logger.warning("Failed to generate proactive post")
            return None

        # Очищаем от кавычек если LLM их добавил
        post_text = post_text.strip().strip('"').strip("'")

        # Проверяем что упомянут таргет
        if target not in post_text:
            post_text += f" {target}"

        logger.info(f"💭 Generated post ({template['name']}, target={target}): {post_text[:80]}...")
        return {
            "text": post_text,
            "template": template["name"],
        }

    async def send_proactive_post(self) -> bool:
        """Отправляет один проактивный пост (если можно)."""
        # Проверка времени
        if not self.is_active_hour():
            now_hour = datetime.now().hour
            logger.debug(f"⏰ Not active hour ({now_hour}h), waiting until {PROACTIVE_ACTIVE_HOUR_START}h")
            return False

        # Проверка дневного лимита
        if not self.can_post_today():
            return False

        # Глобальный дневной бюджет действий (proactive + reply).
        try:
            from anti_ban_module import AntiBanManager
            _ban = AntiBanManager(self.db)
            if not _ban.has_daily_action_budget(self.agent_id):
                return False
            if _ban.is_agent_at_risk(self.agent_id):
                logger.info(f"🛡️ Agent {self.agent_id} at risk — skipping proactive post")
                return False
        except Exception as e:
            logger.debug(f"budget check failed: {e}")

        # Выбираем группу
        group = self.select_target_group()
        if not group:
            return False

        # Генерируем пост (язык по группе - попробуем определить)
        # Простая эвристика: если в названии группы есть кириллица — русский
        title = group.get("title", "")
        has_cyrillic = any('А' <= c <= 'я' for c in title)
        language = "ru" if has_cyrillic else "en"

        post_data = await self.generate_post(language=language)
        if not post_data:
            return False

        # Отправляем
        try:
            telegram_group_id = group["telegram_group_id"]

            # Deep-link атрибуция (bot и channel через gatekeeper).
            # Variant id для A/B зашиваем как _v{V}, чтобы signup_sources
            # привязывал конверсии к шаблону поста.
            target = self.llm.referral_target
            target_type = getattr(self.llm, "target_type", "bot")
            v = post_data.get("template") or "tpl"  # имя шаблона как vid
            if target_type == "bot" and target and target.startswith("@"):
                bot_name = target.lstrip("@")
                payload = f"ag{self.agent_id}_g{group['id']}_v{v}"
                # Mini App: ?startapp= (не ?start=) — иначе start_param не дойдёт.
                try:
                    from config_agent import REFERRAL_MINIAPP_SHORTNAME as _sn
                except Exception:
                    _sn = ""
                if _sn:
                    deeplink = f"https://t.me/{bot_name}/{_sn}?startapp={payload}"
                else:
                    deeplink = f"https://t.me/{bot_name}?startapp={payload}"
                post_data["text"] = post_data["text"].replace(target, deeplink)
            elif target_type == "channel":
                try:
                    from config_agent import CHANNEL_GATEKEEPER_BOT, CHANNEL_INVITE_LINK
                except Exception:
                    CHANNEL_GATEKEEPER_BOT = ""
                    CHANNEL_INVITE_LINK = ""
                if CHANNEL_GATEKEEPER_BOT:
                    gk = CHANNEL_GATEKEEPER_BOT.lstrip("@")
                    link = f"https://t.me/{gk}?start=chag{self.agent_id}_g{group['id']}_v{v}"
                elif CHANNEL_INVITE_LINK:
                    link = CHANNEL_INVITE_LINK
                elif target and target.startswith("@"):
                    link = f"https://t.me/{target.lstrip('@')}"
                else:
                    link = None
                if link and target:
                    post_data["text"] = post_data["text"].replace(target, link)

            logger.info(f"📤 Posting to {group['title']}: {post_data['text'][:60]}...")

            await self.client.send_message(telegram_group_id, post_data["text"])

            # Логируем в БД
            self.db.log_proactive_post(
                agent_id=self.agent_id,
                group_id=group["id"],
                post_text=post_data["text"],
                template_used=post_data["template"]
            )

            # Помечаем группу как активную (если ещё joined)
            if group.get("status") == "joined":
                self.db.update_group_status(group["id"], "active")

            self.last_post_time = datetime.now()

            logger.info(f"✅ Proactive post sent to {group['title']}")
            return True

        except PyrogramException as e:
            err_str = str(e)
            if "CHAT_WRITE_FORBIDDEN" in err_str:
                logger.warning(f"❌ No write permission in {group['title']}")
                self.db.update_group_status(group["id"], "no_permission")
                error_code = "CHAT_WRITE_FORBIDDEN"
            elif "SLOWMODE_WAIT" in err_str:
                wait_sec = getattr(e, 'value', 0) or 0
                cooldown_sec = max(int(wait_sec * SLOWMODE_BACKOFF_MULTIPLIER), SLOWMODE_MIN_COOLDOWN_SEC)
                import time as _t
                _slowmode_cooldowns[group["id"]] = _t.time() + cooldown_sec
                logger.warning(f"❌ Slowmode {wait_sec}s in {group['title']} — cooldown {cooldown_sec}s")
                error_code = f"SLOWMODE_WAIT:{wait_sec}"
            elif "FLOOD_WAIT" in err_str:
                error_code = f"FLOOD_WAIT:{getattr(e, 'value', 0)}"
            elif "USER_BANNED_IN_CHANNEL" in err_str or "USER_KICKED" in err_str:
                error_code = "USER_BANNED"
                # Realtime-алерт в админ-бот
                notify_ban(
                    agent_id=self.agent_id, agent_label="proactive",
                    group_db_id=group.get("id") or 0,
                    group_title=group.get("title", "") or "?",
                    error_code="USER_BANNED_IN_CHANNEL",
                    last_message=post_data.get("text", ""),
                    kind="ban",
                )
            elif "CHANNEL_INVALID" in err_str:
                logger.warning(f"❌ CHANNEL_INVALID in {group['title']} — marking no_permission")
                self.db.update_group_status(group["id"], "no_permission")
                error_code = "CHANNEL_INVALID"
            elif "CHANNEL_PRIVATE" in err_str:
                logger.warning(f"❌ CHANNEL_PRIVATE in {group['title']} — marking private")
                self.db.update_group_status(group["id"], "private")
                error_code = "CHANNEL_PRIVATE"
            elif "ALLOW_PAYMENT_REQUIRED" in err_str:
                logger.warning(f"❌ Paid-only in {group['title']} — marking no_permission")
                self.db.update_group_status(group["id"], "no_permission")
                error_code = "PAID_REQUIRED"
            else:
                logger.error(f"❌ Pyrogram error: {e}")
                error_code = f"RPC:{err_str[:60]}"
            try:
                self.db.log_interaction(
                    agent_id=self.agent_id, group_id=group["id"],
                    message_text="", response_text=post_data["text"],
                    status="failed", error_code=error_code,
                )
                # Авто-disable если стабильно режут
                fails = self.db.count_recent_consecutive_failures(group["id"])
                if fails >= AUTO_DISABLE_AFTER_FAILS and group.get("status") != "no_permission":
                    self.db.update_group_status(group["id"], "no_permission")
                    logger.warning(f"⛔ Auto-disabled {group['title']} after {fails} consecutive failures")
            except Exception:
                pass
            return False

        except Exception as e:
            logger.error(f"❌ Error sending proactive post: {e}")
            try:
                self.db.log_interaction(
                    agent_id=self.agent_id, group_id=group["id"],
                    message_text="", response_text=post_data["text"],
                    status="failed", error_code=f"EXC:{type(e).__name__}",
                )
            except Exception:
                pass
            return False

    async def run_proactive_loop(self):
        """Главный цикл проактивного постинга. Запускается параллельно."""
        if not PROACTIVE_ENABLED:
            logger.info("Proactive mode disabled in config")
            return

        logger.info(f"🚀 Proactive mode ACTIVE")
        logger.info(f"   Max posts/day: {PROACTIVE_MAX_POSTS_PER_DAY}")
        logger.info(f"   Interval: {PROACTIVE_MIN_INTERVAL_HOURS}-{PROACTIVE_MAX_INTERVAL_HOURS}h")
        logger.info(f"   Group cooldown: {PROACTIVE_GROUP_COOLDOWN_DAYS}d")
        logger.info(f"   Active hours: {PROACTIVE_ACTIVE_HOUR_START}-{PROACTIVE_ACTIVE_HOUR_END}h")

        # При старте ждём случайную задержку (от 5 до 30 минут)
        initial_delay = random.randint(300, 1800)
        logger.info(f"⏳ Initial proactive delay: {initial_delay}s ({initial_delay//60}min)")
        await asyncio.sleep(initial_delay)

        while True:
            try:
                # Пытаемся отправить пост
                sent = await self.send_proactive_post()

                # Случайная задержка до следующего поста
                hours = random.uniform(PROACTIVE_MIN_INTERVAL_HOURS, PROACTIVE_MAX_INTERVAL_HOURS)
                delay = int(hours * 3600)

                if sent:
                    logger.info(f"💤 Next proactive post in {hours:.1f}h ({delay}s)")
                else:
                    # Если не отправили (лимит/нет групп) - проверяем чаще
                    delay = 1800  # 30 минут
                    logger.debug(f"💤 Will retry proactive in {delay}s")

                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                logger.info("Proactive loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in proactive loop: {e}", exc_info=True)
                await asyncio.sleep(600)  # 10 минут при ошибке
