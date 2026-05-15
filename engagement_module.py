import logging
import re
from typing import Dict, Optional
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import RPCError as PyrogramException
from agent_database import AgentDatabase
from llm_analyzer import LLMAnalyzer
from config_agent import (
    REFERRAL_BOT, REPLY_VIA_DM, DM_FALLBACK_TO_GROUP,
    REPLY_GROUP_INCLUDES_LINK, LISTENING_INTEREST_THRESHOLD,
    GROUP_TITLE_BLACKLIST, SLOWMODE_BACKOFF_MULTIPLIER, SLOWMODE_MIN_COOLDOWN_SEC,
    CTA_VARIANTS, CHANNEL_GATEKEEPER_BOT, CHANNEL_INVITE_LINK,
)
# shared cooldown dict с proactive_module
from proactive_module import _slowmode_cooldowns
from admin_notifier import notify_ban

logger = logging.getLogger(__name__)

# Хардкодное правило: обвинения в скаме + промо-ссылка = почти гарантированный бан в крипто-группах.
# Если в исходящем тексте есть И обвинение, И промо-указатель — блокируем отправку.
ACCUSATION_PATTERNS = [
    r"\bscam\b",
    r"\bscammer\b",
    r"\bfraud\b",
    r"\bfake\b",
    r"doesn'?t\s+work",
    r"don'?t\s+trust",
    r"don'?t\s+fall\s+for",
    r"\bразвод\b",
    r"\bкидал",
    r"\bмошен",
    r"\bобман",
    r"\bне\s+работает\b",
    r"\bне\s+верьте\b",
    r"\bне\s+ведитесь\b",
    r"\bлохотрон",
]
PROMO_PATTERNS = [
    r"@\w+_?bot\b",
    r"@\w+",
    r"https?://",
    r"t\.me/",
]
ACCUSATION_RE = re.compile("|".join(ACCUSATION_PATTERNS), re.IGNORECASE)
PROMO_RE = re.compile("|".join(PROMO_PATTERNS), re.IGNORECASE)


class EngagementModule:
    """Модуль для взаимодействия с пользователями и отправки предложений."""

    def __init__(self, client: Client, db: AgentDatabase, llm: LLMAnalyzer, agent_id: int):
        self.client = client
        self.db = db
        self.llm = llm
        self.agent_id = agent_id

    async def create_offer(self, original_message: str, group_context: str = "") -> Optional[str]:
        """
        Генерирует персонализированное предложение.

        Args:
            original_message: Исходное сообщение пользователя
            group_context: Контекст группы

        Returns:
            Сгенерированное предложение или None
        """
        try:
            # Безопасный preview для лога
            safe_text = str(original_message) if original_message else ""
            preview = safe_text[:50] if len(safe_text) > 50 else safe_text
            logger.debug(f"Creating offer for message: {preview}...")

            # Используем LLM для генерации ответа
            response = self.llm.generate_response(original_message, group_context)

            if response:
                # Убеждаемся, что в ответе есть упоминание платформы, если это уместно
                if "@" not in response:  # Если нет упоминания бота
                    # Может понадобиться добавить упоминание
                    analysis = self.llm.analyze_message(original_message)
                    if analysis["interested"]:
                        response = f"{response}\n\nОтправьте сообщение боту {REFERRAL_BOT} для начала."

                logger.info(f"Offer created: {response[:100]}...")
                return response
            else:
                logger.warning("Failed to generate offer")
                return None

        except Exception as e:
            logger.error(f"Error creating offer: {e}")
            return None

    async def send_message(self, group_id: int, message_text: str):
        """Возвращает (success: bool, error_code: Optional[str])."""
        """
        Отправляет сообщение в группу.

        Перед отправкой проверяет текст против выученных правил (ban_lessons).
        Если найдено запрещённое слово — НЕ отправляет (защита от повторного бана).

        Args:
            group_id: ID группы (Telegram chat_id)
            message_text: Текст сообщения

        Returns:
            True если успешно отправлено
        """
        try:
            if not message_text or not message_text.strip():
                logger.warning("Empty message text")
                return False, "EMPTY"

            # === SOFTENED BLOCK: обвинение + промо-ссылка ===
            # Раньше: silent drop всего сообщения.
            # Теперь: пытаемся вырезать обвинительные фразы и оставить промо.
            # Если после очистки от текста почти ничего не осталось — только тогда блок.
            acc = ACCUSATION_RE.search(message_text)
            promo = PROMO_RE.search(message_text)
            if acc and promo:
                cleaned = ACCUSATION_RE.sub("", message_text)
                # схлопываем двойные пробелы и пунктуацию
                cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.!?;:-")
                if len(cleaned) >= 20 and PROMO_RE.search(cleaned):
                    logger.info(
                        f"🧹 Стрипнул обвинения ('{acc.group(0)}'), отправляю очищенный текст. "
                        f"До: '{message_text[:100]}' | После: '{cleaned[:100]}'"
                    )
                    message_text = cleaned
                else:
                    logger.warning(
                        f"🛡️ Блок: обвинение ('{acc.group(0)}') + промо ('{promo.group(0)}'), "
                        f"очистить нечем. Текст: '{message_text[:120]}...'"
                    )
                    return False, "ACCUSATION_PROMO_BLOCK"

            # === LEARNED RULES CHECK ===
            # Проверяем против правил выученных из прошлых банов
            try:
                matched = self.db.check_text_against_lessons(message_text)
                if matched:
                    phrases = [m['forbidden_phrase'] for m in matched]
                    logger.warning(
                        f"🛡️ Сообщение заблокировано фильтром! "
                        f"Найдены опасные фразы: {phrases}. "
                        f"Сообщение: '{message_text[:80]}...'"
                    )
                    # Логируем что правила сработали
                    for m in matched:
                        try:
                            self.db.increment_lesson_trigger(m['id'])
                        except Exception:
                            pass
                    return False, "FILTER_BLOCK"
            except Exception as e:
                logger.debug(f"Lesson check error: {e}")

            logger.info(f"Sending message to group {group_id}: {message_text[:50]}...")

            # Отправляем сообщение
            sent_message = await self.client.send_message(group_id, message_text)

            logger.info(f"Message sent successfully to group {group_id}")
            return True, None

        except PyrogramException as e:
            err_str = str(e)

            # Helper для обновления статуса группы по telegram_group_id.
            # Возвращает (db_id, title) для последующих алертов.
            matched_group: Dict = {}

            def mark_group(new_status: str):
                groups = self.db.get_groups_by_statuses(
                    ["joined", "active", "discovered"], limit=1000
                )
                for g in groups:
                    if g.get("telegram_group_id") == group_id:
                        self.db.update_group_status(g["id"], new_status)
                        try:
                            self.db.set_membership(self.agent_id, g["id"], new_status, err_str)
                        except Exception:
                            pass
                        matched_group["id"] = g["id"]
                        matched_group["title"] = g.get("title", "")
                        return g["id"]
                return None

            error_code = "RPC_OTHER"
            if "CHAT_WRITE_FORBIDDEN" in err_str:
                logger.warning(f"No permission to write in group {group_id}")
                mark_group("no_permission")
                error_code = "CHAT_WRITE_FORBIDDEN"
            elif "USER_BANNED_IN_CHANNEL" in err_str or "USER_KICKED" in err_str:
                logger.warning(f"⛔ Banned in group {group_id} — blacklisting globally")
                gid = mark_group("banned")
                if gid:
                    try:
                        self.db.blacklist_group(gid, reason=err_str[:200])
                    except Exception as e:
                        logger.debug(f"blacklist_group error: {e}")
                try:
                    self._auto_learn_from_ban(message_text, group_id, err_str)
                except Exception as e:
                    logger.debug(f"Auto-learn error: {e}")
                # Realtime-алерт в админ-бот
                notify_ban(
                    agent_id=self.agent_id, agent_label="reply",
                    group_db_id=matched_group.get("id") or 0,
                    group_title=matched_group.get("title", "") or str(group_id),
                    error_code="USER_BANNED_IN_CHANNEL",
                    last_message=message_text,
                    kind="ban",
                )
                error_code = "USER_BANNED"
            elif "CHANNEL_PRIVATE" in err_str:
                logger.warning(f"Group {group_id} is private or we're kicked")
                mark_group("private")
                error_code = "CHANNEL_PRIVATE"
            elif "CHANNEL_INVALID" in err_str:
                # Группа удалена/недоступна — больше не дёргать.
                logger.warning(f"Group {group_id}: CHANNEL_INVALID — marking no_permission")
                mark_group("no_permission")
                error_code = "CHANNEL_INVALID"
            elif "ALLOW_PAYMENT_REQUIRED" in err_str:
                # Paid messages required — обычные юзеры писать не могут.
                logger.warning(f"Group {group_id}: paid-only — marking no_permission")
                mark_group("no_permission")
                error_code = "PAID_REQUIRED"
            elif "SLOWMODE_WAIT" in err_str:
                wait_sec = getattr(e, 'value', 0) or 0
                cooldown_sec = max(int(wait_sec * SLOWMODE_BACKOFF_MULTIPLIER), SLOWMODE_MIN_COOLDOWN_SEC)
                # Записываем cooldown по db_group_id, если найдём
                try:
                    groups = self.db.get_groups_by_statuses(["joined", "active"], limit=2000)
                    for g in groups:
                        if g.get("telegram_group_id") == group_id:
                            import time as _t
                            _slowmode_cooldowns[g["id"]] = _t.time() + cooldown_sec
                            break
                except Exception:
                    pass
                logger.warning(f"⏳ Slow mode {wait_sec}s in {group_id} — cooldown {cooldown_sec}s")
                error_code = f"SLOWMODE_WAIT:{wait_sec}"
            elif "FLOOD_WAIT" in err_str:
                wait_sec = getattr(e, 'value', 0)
                logger.warning(f"⏳ FloodWait {wait_sec}s for {group_id} — skipping")
                error_code = f"FLOOD_WAIT:{wait_sec}"
            else:
                logger.error(f"Pyrogram error sending message: {e}")
                error_code = f"RPC:{err_str[:60]}"
            return False, error_code

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False, f"EXC:{type(e).__name__}"

    async def send_direct_message(self, user_id: int, message_text: str) -> bool:
        """
        Отправляет личное сообщение пользователю.

        Args:
            user_id: ID пользователя
            message_text: Текст сообщения

        Returns:
            True если успешно отправлено
        """
        try:
            logger.info(f"Sending DM to user {user_id}")

            await self.client.send_message(user_id, message_text)

            logger.info(f"DM sent successfully to user {user_id}")
            return True

        except PyrogramException as e:
            logger.warning(f"Cannot send DM to user {user_id}: {e}")
            return False

        except Exception as e:
            logger.error(f"Error sending DM: {e}")
            return False

    async def log_interaction(self, group_id: int, message_text: str,
                             response_text: str, user_id: Optional[int] = None,
                             status: str = 'sent', error_code: Optional[str] = None,
                             cta_variant: Optional[str] = None) -> bool:
        """
        Логирует взаимодействие в БД.

        Args:
            group_id: ID группы в нашей БД
            message_text: Исходное сообщение пользователя
            response_text: Наш ответ
            user_id: ID пользователя (если известен)
            status: 'sent' / 'failed' / 'pending'

        Returns:
            True если успешно залогировано
        """
        try:
            interaction_id = self.db.log_interaction(
                agent_id=self.agent_id,
                group_id=group_id,
                message_text=message_text,
                response_text=response_text,
                user_id=user_id,
                status=status,
                error_code=error_code,
                cta_variant=cta_variant,
            )

            logger.debug(f"Interaction logged: {interaction_id} (status={status})")
            return True

        except Exception as e:
            logger.error(f"Error logging interaction: {e}")
            return False

    async def handle_message(self, message: Message, group_db_id: int, safe_text: Optional[str] = None) -> bool:
        """
        Обрабатывает сообщение полностью (анализирует, генерирует ответ, отправляет).

        Args:
            message: Объект сообщения от Pyrogram
            group_db_id: ID группы в нашей БД
            safe_text: Предварительно очищенный текст (если передан - используем его)

        Returns:
            True если ответ был отправлен
        """
        try:
            # Берём очищенный текст, или конвертируем сами
            if safe_text is not None:
                message_text = safe_text
            else:
                raw_text = message.text or message.caption or ""
                try:
                    message_text = raw_text.encode("utf-8", "ignore").decode("utf-8") if raw_text else ""
                except Exception:
                    message_text = ""

            group_id = message.chat.id
            user_id = message.from_user.id if message.from_user else None

            if not message_text.strip():
                return False

            # Не отвечаем в каналах (там только админы пишут)
            chat_type = getattr(message.chat.type, "name", "").upper()
            if chat_type == "CHANNEL":
                logger.debug(f"Skipping channel {group_id} (read-only)")
                return False

            # Blacklist: не отвечаем в опасных нишах (детские/игровые/политика)
            chat_title_lower = (message.chat.title or "").lower()
            if any(b in chat_title_lower for b in GROUP_TITLE_BLACKLIST):
                logger.info(f"⛔ Skip blacklisted group: {message.chat.title}")
                return False

            # Если по этой группе активен slow-mode cooldown — пропускаем
            try:
                import time as _t
                if _slowmode_cooldowns.get(group_db_id, 0) > _t.time():
                    logger.debug(f"⏳ Slow-mode cooldown active for group_db {group_db_id} — skip reply")
                    return False
            except Exception:
                pass

            # Анализируем сообщение
            analysis = self.llm.analyze_message(message_text)

            # Решаем отвечать ли
            if analysis["interest_level"] < LISTENING_INTEREST_THRESHOLD:
                logger.debug(f"Message not relevant enough: {analysis['interest_level']} < {LISTENING_INTEREST_THRESHOLD}")
                return False

            logger.info(f"Processing relevant message from user {user_id}")

            # Генерируем ответ
            try:
                raw_title = message.chat.title or "Unknown"
                chat_title = raw_title.encode("utf-8", "ignore").decode("utf-8")
            except Exception:
                chat_title = "Unknown"
            response = await self.create_offer(message_text, f"Group: {chat_title}")

            if not response:
                logger.warning("Failed to generate response")
                return False

            # Хук в группе генерим до DM, чтобы знать variant_id и зашить его
            # И в групповой CTA, И в DM-диплинк → CTR считается единым.
            hook_text, cta_variant = (None, None)
            if REPLY_VIA_DM and user_id and REPLY_GROUP_INCLUDES_LINK:
                hook_text, cta_variant = self._make_group_hook(
                    message.from_user, group_db_id=group_db_id, include_link=True,
                )

            # Deep-link атрибуция: подменяем @target на t.me/...?start=...
            response = self._inject_deeplink(response, group_db_id, variant_id=cta_variant)

            # ЛС-режим: отвечаем в личку (там нет модерации, конверсия выше).
            # В группе оставляем короткий хук без ссылки чтобы не сжечь акк.
            success = False
            error_code = None
            sent_to_dm = False

            if REPLY_VIA_DM and user_id:
                dm_ok = await self.send_direct_message(user_id, response)
                if dm_ok:
                    sent_to_dm = True
                    success = True
                    # Хук в группе. Если REPLY_GROUP_INCLUDES_LINK=True —
                    # hook_text уже сгенерён выше с диплинком.
                    if not hook_text:
                        hook_text, _ = self._make_group_hook(
                            message.from_user, group_db_id=group_db_id, include_link=False,
                        )
                    if hook_text:
                        try:
                            await self.client.send_message(
                                group_id, hook_text,
                                reply_to_message_id=message.id,
                            )
                        except Exception as e:
                            logger.debug(f"Group hook send error: {e}")
                else:
                    # DM запрещён — fallback в группу (если разрешено)
                    if DM_FALLBACK_TO_GROUP:
                        success, error_code = await self.send_message(group_id, response)
                    else:
                        error_code = "DM_FORBIDDEN_NO_FALLBACK"
            else:
                success, error_code = await self.send_message(group_id, response)

            # Логируем взаимодействие
            await self.log_interaction(
                group_id=group_db_id,
                message_text=message_text,
                response_text=("[DM] " if sent_to_dm else "") + response,
                user_id=user_id,
                status='sent' if success else 'failed',
                error_code=error_code,
                cta_variant=cta_variant,
            )

            # Если успешно отправили - помечаем группу как 'active' (если ещё не active/banned)
            if success:
                try:
                    groups = self.db.get_target_groups(status="joined", limit=1000)
                    for g in groups:
                        if g.get("id") == group_db_id:
                            self.db.update_group_status(group_db_id, "active")
                            logger.debug(f"Group {group_db_id} marked as active")
                            break
                except Exception as e:
                    logger.debug(f"Could not update group status: {e}")

            return success

        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return False

    def _pick_cta_variant(self, has_name: bool) -> tuple:
        """Возвращает (variant_id, template) под текущий target_type.

        Pool — CTA_VARIANTS[type]['named'|'anon']. Случайный выбор.
        variant_id логируется в interactions.cta_variant и зашивается в диплинк
        как ..._v{ID} → signup_sources.cta_variant. CTR = signups/sends по id.
        """
        import random
        ttype = getattr(self.llm, "target_type", "bot")
        pool = CTA_VARIANTS.get(ttype) or CTA_VARIANTS.get("bot")
        variants = pool["named"] if has_name and pool.get("named") else pool["anon"]
        return random.choice(variants)

    def _make_group_hook(self, from_user, group_db_id: Optional[int] = None,
                         include_link: bool = False) -> tuple:
        """Короткий хук в группе. Возвращает (text, variant_id|None).

        include_link=False: только намёк на ЛС, без ссылок — variant_id=None.
        include_link=True: рендерит CTA из CTA_VARIANTS с диплинком; variant_id
        логируется и зашит в диплинк (_v{ID}) для CTR-аналитики.
        """
        import random
        name = ""
        try:
            if from_user and from_user.first_name:
                name = from_user.first_name.split()[0]
        except Exception:
            pass

        if include_link:
            variant_id, template = self._pick_cta_variant(has_name=bool(name))
            link = self._build_deeplink(group_db_id, variant_id=variant_id)
            if link:
                return template.format(name=name, link=link), variant_id
            # link недоступен — fallback на старый хук без ссылки

        variants_named = [
            f"{name}, скинул в лс — там подробнее",
            f"{name}, написал тебе в личку",
            f"{name}, ответил в лс чтоб не флудить",
        ]
        variants_anon = [
            "скинул в лс",
            "написал в личку",
            "отправил в лс",
        ]
        return random.choice(variants_named if name else variants_anon), None

    def _build_deeplink(self, group_db_id: Optional[int],
                        variant_id: Optional[str] = None) -> Optional[str]:
        """Собирает ссылку с атрибуцией под текущий target_type.

        bot:     t.me/<bot>?start=ag{N}_g{GID}[_v{V}]
        channel: t.me/<gatekeeper>?start=chag{N}_g{GID}[_v{V}]  если задан
                 gatekeeper, иначе CHANNEL_INVITE_LINK или t.me/<channel>
        group:   t.me/<group>
        """
        try:
            target = getattr(self.llm, "referral_target", REFERRAL_BOT) or REFERRAL_BOT
            target_type = getattr(self.llm, "target_type", "bot")
            gid = group_db_id if group_db_id is not None else 0
            v_suffix = f"_v{variant_id}" if variant_id else ""

            if target_type == "bot" and target.startswith("@"):
                bot_name = target.lstrip("@")
                return f"https://t.me/{bot_name}?start=ag{self.agent_id}_g{gid}{v_suffix}"

            if target_type == "channel":
                if CHANNEL_GATEKEEPER_BOT:
                    gk = CHANNEL_GATEKEEPER_BOT.lstrip("@")
                    return f"https://t.me/{gk}?start=chag{self.agent_id}_g{gid}{v_suffix}"
                if CHANNEL_INVITE_LINK:
                    return CHANNEL_INVITE_LINK
                if target.startswith("@"):
                    return f"https://t.me/{target.lstrip('@')}"
                return None

            if target_type == "group" and target.startswith("@"):
                return f"https://t.me/{target.lstrip('@')}"
            return None
        except Exception:
            return None

    def _inject_deeplink(self, text: str, group_db_id: int,
                         variant_id: Optional[str] = None) -> str:
        """Подменяет @target_username на полный deep-link с UTM для атрибуции.

        Работает для bot и channel (через gatekeeper). Для group — оставляет
        @username (Telegram сам сделает кликабельным). variant_id зашивается в
        payload для CTR-аналитики.
        """
        if not text:
            return text
        try:
            target = getattr(self.llm, "referral_target", REFERRAL_BOT) or REFERRAL_BOT
            target_type = getattr(self.llm, "target_type", "bot")
            if target_type not in ("bot", "channel") or not target.startswith("@"):
                return text
            link = self._build_deeplink(group_db_id, variant_id=variant_id)
            if not link:
                return text
            return text.replace(target, link)
        except Exception:
            return text

    def _auto_learn_from_ban(self, banned_text: str, group_id: int, error: str):
        """Извлекает уроки из забаненного сообщения.

        Если в тексте есть слова из списка "опасных" — добавляет в ban_lessons.
        """
        if not banned_text:
            return

        suspicious_patterns = [
            # Английские
            "scam", "fraud", "fake", "easy money", "guaranteed",
            "doesn't work", "this is a scam", "100% real", "instant",
            "flash crypto", "flash btc",
            # Русские
            "развод", "лохотрон", "обман", "лохи", "это скам",
            "халява", "100% работает", "100% реально", "гарантирую",
        ]

        text_lower = banned_text.lower()
        # Узнаём название группы
        group_title = ""
        try:
            groups = self.db.get_groups_by_statuses(["banned", "active", "joined"], limit=1000)
            for g in groups:
                if g.get("telegram_group_id") == group_id:
                    group_title = g.get("title", "")
                    break
        except Exception:
            pass

        # Топик группы
        title_lower = group_title.lower()
        topic = "general"
        if any(w in title_lower for w in ['crypto', 'bitcoin', 'btc', 'крипт']):
            topic = "crypto"
        elif any(w in title_lower for w in ['работ', 'work', 'job', 'фриланс']):
            topic = "work"

        learned = []
        for pattern in suspicious_patterns:
            if pattern in text_lower:
                lid = self.db.add_ban_lesson(
                    forbidden_phrase=pattern,
                    error_type="USER_BANNED_IN_CHANNEL",
                    topic=topic,
                    recommendation=f"Слово '{pattern}' привело к бану в '{group_title[:50]}'. Избегать.",
                    source_group_title=group_title,
                    source_message=banned_text[:300],
                    auto_learned=True,
                )
                if lid:
                    learned.append(pattern)

        if learned:
            logger.warning(f"📚 LEARNED новые опасные фразы: {learned}")

    def get_engagement_stats(self) -> Dict:
        """Возвращает статистику взаимодействий."""
        try:
            interactions = self.db.get_interactions(agent_id=self.agent_id, limit=100)

            total = len(interactions)
            successful = len([i for i in interactions if i.get("status") == "success"])
            failed = len([i for i in interactions if i.get("status") == "failed"])
            pending = len([i for i in interactions if i.get("status") == "pending"])

            return {
                "total_interactions": total,
                "successful": successful,
                "failed": failed,
                "pending": pending,
                "success_rate": successful / total if total > 0 else 0
            }

        except Exception as e:
            logger.error(f"Error getting engagement stats: {e}")
            return {}
