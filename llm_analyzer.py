import requests
import json
import logging
from typing import Dict, Optional
from config_agent import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL, AGENT_PROMPT_TEMPLATE, REFERRAL_BOT

logger = logging.getLogger(__name__)


# Однозначно мужские имена с окончанием на 'а'/'я' (исключения из правила
# "оканчивается на а/я → женское").
_MALE_NAMES_AYA = {
    'никита', 'илья', 'данила', 'кузьма', 'фома', 'савва', 'лёва', 'лева',
    'миша', 'паша', 'саша', 'женя', 'валя', 'юра', 'ваня',  # короткие — неоднозначны, но чаще мужские
}
# Унисекс короткие формы — лучше считать неопределёнными.
_AMBIGUOUS_NAMES = {'саша', 'женя', 'валя', 'слава'}
# Однозначно женские имена латиницей (агенты с не-кириллическими именами).
_FEMALE_NAMES_LATIN = {
    'anna', 'anya', 'maria', 'masha', 'julia', 'yulia', 'olga', 'elena',
    'lena', 'natasha', 'natalia', 'kate', 'katya', 'svetlana', 'sveta',
    'irina', 'ira', 'tanya', 'tatiana', 'darya', 'dasha', 'alina', 'polina',
    'sophia', 'sofia', 'sonia', 'sonya', 'ekaterina', 'liza', 'lisa',
    'oksana', 'ksenia', 'kseniya', 'vera', 'nina', 'lyuba', 'lyubov',
    'marina', 'angelina', 'valeria', 'lera', 'kristina', 'christina',
    'viktoria', 'victoria', 'vika', 'evgenia', 'evgeniya',
}


def detect_gender(first_name: Optional[str]) -> str:
    """Эвристика определения пола по имени. Возвращает 'female' / 'male' / 'unknown'."""
    if not first_name:
        return 'unknown'
    name = first_name.strip().lower().split()[0] if first_name.strip() else ''
    if not name:
        return 'unknown'
    if name in _AMBIGUOUS_NAMES:
        return 'unknown'
    if name in _FEMALE_NAMES_LATIN:
        return 'female'
    # Кириллица: оканчивается на а/я (с исключениями)
    if name[-1] in ('а', 'я'):
        if name in _MALE_NAMES_AYA:
            return 'male'
        return 'female'
    # Латинские женские с -a (Anna, Maria, Julia ...): уже в whitelist, но
    # допускаем общее правило для не учтённых.
    if name.isascii() and name.endswith('a') and len(name) >= 4:
        return 'female'
    return 'male'


class LLMAnalyzer:
    """LLM анализатор для работы с OpenRouter и бесплатными моделями."""

    # Fallback модели - если основная не работает, пробуем эти
    FALLBACK_MODELS = [
        "minimax/minimax-m2.5:free",
        "baidu/cobuddy:free",
        "tencent/hy3-preview:free",
        "poolside/laguna-m.1:free",
        "poolside/laguna-xs.2:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    ]

    def __init__(self, api_key: str = OPENROUTER_API_KEY, model: str = OPENROUTER_MODEL,
                 referral_target: Optional[str] = None):
        self.api_key = api_key
        self.model = model
        self.base_url = OPENROUTER_BASE_URL

        # Цель которую этот агент промоутит (бот или группа)
        from config_agent import REFERRAL_TARGETS
        self.referral_target = referral_target or REFERRAL_BOT

        # Описание из конфига для LLM
        target_info = REFERRAL_TARGETS.get(self.referral_target, {})
        self.target_type = target_info.get("type", "bot")
        self.target_description = target_info.get("description", "")
        self.target_cta = target_info.get("call_to_action", "Попробуй {target}").format(target=self.referral_target)

        # Гендер агента (определяется позже через apply_persona). По умолчанию unknown.
        self.gender: str = 'unknown'
        self.first_name: Optional[str] = None

        # Загружаем выученные правила из БД (если доступно)
        self._forbidden_phrases_str = self._load_forbidden_phrases()

        self.agent_prompt = self._build_agent_prompt()

        # Список моделей: основная + fallback (без дублей)
        self.models_to_try = [model] + [m for m in self.FALLBACK_MODELS if m != model]

        if not self.api_key:
            logger.warning("OPENROUTER_API_KEY не установлен! LLM функции не будут работать.")
        else:
            logger.info(f"LLM analyzer initialized: target={self.referral_target} (type={self.target_type})")

    def _build_agent_prompt(self) -> str:
        """Собирает системный prompt с учётом target и gender."""
        kind_word = "группу" if self.target_type == "group" else "бот"
        target_desc = self.target_description

        if self.gender == 'female':
            persona_line = (
                f"Ты — женщина, обычная пользовательница Telegram (имя: {self.first_name or 'без имени'}). "
                "О себе пиши в ЖЕНСКОМ роде: 'пробовала', 'получалось у меня', 'сама использую', "
                "'я бы посоветовала'. НЕ путай род — это палится."
            )
            examples = (
                '- "я сама пробовала X, нормально"\n'
                '- "у меня получалось через X"\n'
                '- "давно пользуюсь X, советую"\n'
                '- "будь осторожен" вместо "это скам"'
            )
        elif self.gender == 'male':
            persona_line = (
                f"Ты — мужчина, обычный пользователь Telegram (имя: {self.first_name or 'без имени'}). "
                "О себе пиши в МУЖСКОМ роде: 'пробовал', 'получалось у меня', 'сам использую'."
            )
            examples = (
                '- "я сам пробовал X, нормально"\n'
                '- "у меня получалось через X"\n'
                '- "давно использую X, советую"\n'
                '- "будь осторожен" вместо "это скам"'
            )
        else:
            persona_line = (
                "Ты — обычный пользователь Telegram. По возможности избегай форм "
                "прошедшего времени с явным родом ('пробовал/пробовала'); "
                "используй настоящее время или нейтральные обороты."
            )
            examples = (
                '- "у меня получается через X"\n'
                '- "пользуюсь X давно, нормально"\n'
                '- "советую попробовать X"\n'
                '- "будь осторожен" вместо "это скам"'
            )

        return f"""
{persona_line}
Когда видишь что кто-то ищет работу/подработку — естественно делишься своим
опытом и упоминаешь {kind_word} {self.referral_target}.

ОПИСАНИЕ:
{target_desc}

ПРАВИЛА СТИЛЯ:
- Пиши как живой человек, а не как продавец
- Коротко (1-3 предложения)
- БЕЗ рекламного тона: никаких "успей!", "уникально!", "ограниченное время!"
- БЕЗ хайпа: никаких "easy money", "100% работает", "гарантирую"
- НЕ критикуй другие проекты словами "scam", "fake", "развод", "лохотрон"
  — это триггерит модерацию и ведёт к бану
- НЕ пиши "там есть бот..." — слишком очевидно как реклама

ВЫУЧЕННЫЕ ИЗ ПРОШЛЫХ БАНОВ ОПАСНЫЕ ФРАЗЫ (НЕ ИСПОЛЬЗОВАТЬ):
{self._forbidden_phrases_str}

ХОРОШИЕ ФОРМУЛИРОВКИ:
{examples}
"""

    def apply_persona(self, first_name: Optional[str]) -> str:
        """Определяет пол по имени и пересобирает agent_prompt. Возвращает gender."""
        self.first_name = first_name
        self.gender = detect_gender(first_name)
        self.agent_prompt = self._build_agent_prompt()
        return self.gender

    def _load_forbidden_phrases(self) -> str:
        """Загружает список запрещённых фраз из БД для системного промпта."""
        try:
            from agent_database import AgentDatabase
            from config_agent import DB_PATH
            db = AgentDatabase(DB_PATH)
            lessons = db.get_ban_lessons(only_enabled=True)
            if not lessons:
                return "(пока нет данных)"
            # Топ-10 самых частых
            top = sorted(lessons, key=lambda l: l.get('ban_count', 0), reverse=True)[:10]
            phrases = [f'"{l["forbidden_phrase"]}"' for l in top]
            return ", ".join(phrases)
        except Exception:
            return "(ошибка загрузки правил)"

    def detect_language(self, text: str) -> str:
        """Определяет язык текста (ru/en)."""
        if not text:
            return "ru"

        # Простой способ определения языка по символам
        cyrillic_count = sum(1 for char in text if 'А' <= char <= 'я' or char in 'ЁёЮюЧч')
        text_length = len([c for c in text if c.isalpha()])

        if text_length > 0 and cyrillic_count / text_length > 0.3:
            return "ru"
        return "en"

    def analyze_message(self, message: str) -> Dict:
        """
        Анализирует сообщение и определяет интерес к заработку.

        Returns:
            Dict с ключами:
            - interested: bool (интересуется ли заработком)
            - interest_level: float (0.0-1.0)
            - language: str (ru/en)
            - intent: str (ищет_работу, спрашивает_совет, жалуется, другое)
        """
        language = self.detect_language(message)

        if language == "ru":
            keywords = ["заработок", "деньги", "подработка", "заработать", "заработаю",
                       "доход", "работа", "удаленк", "фриланс", "задани", "кидки",
                       "крипто", "монет", "токен", "биржа", "лучше", "можно"]
            intent_keywords = {
                "ищет_работу": ["ищу", "ищу работ", "нужна работа", "хочу заработ", "помогите заработ"],
                "спрашивает_совет": ["как заработ", "где заработ", "способ", "метод", "совет", "знаешь"],
                "жалуется": ["денег нет", "не хватает", "финансовые проблемы", "плохо", "тяжело"]
            }
        else:
            keywords = ["earn", "money", "income", "job", "work", "task", "freelance",
                       "remote", "crypto", "coin", "token", "make", "crypto", "side hustle"]
            intent_keywords = {
                "ищет_работу": ["looking for", "need work", "want to earn", "seeking"],
                "спрашивает_совет": ["how to earn", "where to earn", "how can i", "advice"],
                "жалуется": ["no money", "broke", "financial problems", "hard", "difficult"]
            }

        msg_lower = message.lower()
        keyword_matches = sum(1 for kw in keywords if kw in msg_lower)
        # Скоринг: каждое совпадение даёт 0.18 (3 слова → 0.54, что > 0.5)
        interest_level = min(1.0, keyword_matches * 0.18)

        # Бонус за эмоциональные триггеры (человек в моменте принятия решения)
        try:
            from config_agent import (
                EMOTIONAL_TRIGGERS_RU, EMOTIONAL_TRIGGERS_EN,
                LISTENING_EMOTIONAL_BONUS,
            )
            triggers = EMOTIONAL_TRIGGERS_RU if language == "ru" else EMOTIONAL_TRIGGERS_EN
            if any(t in msg_lower for t in triggers):
                interest_level = min(1.0, interest_level + LISTENING_EMOTIONAL_BONUS)
        except Exception:
            pass

        interested = interest_level > 0.2

        intent = "другое"
        for intent_type, intent_kws in intent_keywords.items():
            if any(kw in msg_lower for kw in intent_kws):
                intent = intent_type
                break

        return {
            "interested": interested,
            "interest_level": interest_level,
            "language": language,
            "intent": intent
        }

    # Таймаут на одну модель. Раньше 30с × 6 моделей = до 3 минут блокировки
    # на одном LLM-вызове, что фактически вешало message handler.
    _PER_MODEL_TIMEOUT = 12
    # После 2 подряд провалов считаем модель "битой" и пропускаем 5 минут.
    _MODEL_COOLDOWN_AFTER_FAILS = 2
    _MODEL_COOLDOWN_SEC = 300
    _model_failures: Dict[str, int] = {}
    _model_skip_until: Dict[str, float] = {}

    def _make_request(self, messages: list, temperature: float = 0.7) -> Optional[str]:
        """Делает запрос к OpenRouter API с автопереключением моделей при ошибке.

        Оптимизации:
        - таймаут на модель 12с вместо 30с (free модели медленнее = бесполезны)
        - модель с 2+ подряд провалами уходит в cooldown на 5 минут (не дёргаем)
        - удачная модель всегда первая в списке
        """
        import time as _t
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://moneymaker.quest",
            "X-Title": "MoneyMaker AI Agent",
        }

        last_error = None
        now = _t.time()

        for model in self.models_to_try:
            # Пропускаем модели в cooldown
            if self._model_skip_until.get(model, 0) > now:
                continue
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 200,
                }

                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self._PER_MODEL_TIMEOUT,
                )

                if response.status_code == 200:
                    result = response.json()
                    if "choices" in result and result["choices"]:
                        self._model_failures[model] = 0
                        if model != self.model:
                            logger.info(f"🔄 Switched to working model: {model}")
                            self.model = model
                            self.models_to_try = [model] + [m for m in self.models_to_try if m != model]
                        return result["choices"][0]["message"]["content"].strip()

                elif response.status_code == 429:
                    logger.warning(f"⚠️  Rate limit on {model}, trying next...")
                    last_error = f"429 on {model}"
                else:
                    logger.warning(f"❌ {model} failed: {response.status_code}")
                    last_error = f"{response.status_code} on {model}"

            except requests.exceptions.Timeout:
                logger.warning(f"⏱️  Timeout >{self._PER_MODEL_TIMEOUT}s on {model}")
                last_error = f"timeout on {model}"
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request error on {model}: {e}")
                last_error = str(e)
            except Exception as e:
                logger.warning(f"Error on {model}: {e}")
                last_error = str(e)

            # Регистрируем фейл этой модели
            self._model_failures[model] = self._model_failures.get(model, 0) + 1
            if self._model_failures[model] >= self._MODEL_COOLDOWN_AFTER_FAILS:
                self._model_skip_until[model] = now + self._MODEL_COOLDOWN_SEC
                logger.info(f"💤 {model} → cooldown {self._MODEL_COOLDOWN_SEC}s after {self._model_failures[model]} fails")

        logger.error(f"❌ All models failed. Last error: {last_error}")
        return None

    # Шаблоны на случай полного отказа LLM — лучше отправить generic-ответ
    # с диплинком, чем потерять hot-lead полностью.
    _FALLBACK_TEMPLATES_RU = [
        "у меня норм идёт через {target} — попробуй, простые задания за usdt",
        "сам(а) сижу на {target}, мелочь капает стабильно — глянь",
        "если ищешь подработку — {target}, там короткие задания с выплатой",
        "посмотри {target}, я отсюда тащу — мне ок зашло",
    ]
    _FALLBACK_TEMPLATES_EN = [
        "I'm on {target}, small tasks paid in usdt — check it out",
        "try {target}, decent for side income",
        "if you're looking for side gigs — {target}, quick tasks with payouts",
    ]

    def _template_fallback(self, language: str) -> str:
        import random as _r
        pool = self._FALLBACK_TEMPLATES_RU if language == "ru" else self._FALLBACK_TEMPLATES_EN
        return _r.choice(pool).format(target=self.referral_target)

    def generate_response(self, original_message: str, user_context: str = "") -> Optional[str]:
        """
        Генерирует естественный ответ на сообщение пользователя.

        Args:
            original_message: Исходное сообщение пользователя
            user_context: Дополнительный контекст (например, информация о группе)

        Returns:
            Сгенерированный ответ или None в случае ошибки
        """
        language = self.detect_language(original_message)

        # Тип target определяет акцент в промпте
        target_word_ru = "группу" if self.target_type == "group" else "бот"
        target_word_en = "group" if self.target_type == "group" else "bot"

        if language == "ru":
            user_prompt = f"""Помогите мне сформулировать дружелюбный и естественный ответ на сообщение в Telegram группе:

Исходное сообщение: "{original_message}"

Контекст: {user_context if user_context else "обычная дискуссия"}

Требования:
- Ответ должен быть коротким (максимум 2-3 предложения)
- Отвечай как обычный пользователь, не как бот
- Предложи {target_word_ru} {self.referral_target} только если это уместно
- {self.target_cta}
- Избегай спама и навязчивости
- Ответ на русском языке

Ответь только текстом ответа, без пояснений:"""
        else:
            user_prompt = f"""Help me formulate a friendly and natural response to a message in a Telegram group:

Original message: "{original_message}"

Context: {user_context if user_context else "regular discussion"}

Requirements:
- Answer should be short (max 2-3 sentences)
- Respond as a regular user, not a bot
- Only suggest the {target_word_en} {self.referral_target} if appropriate
- Avoid spam and pushy sales
- Answer in English

Reply with just the response text, no explanations:"""

        messages = [
            {"role": "system", "content": self.agent_prompt},
            {"role": "user", "content": user_prompt}
        ]

        response = self._make_request(messages, temperature=0.7)

        if response:
            logger.info(f"Generated response: {response[:100]}...")
            return response

        # Fallback: LLM полностью лёг — лучше шаблон с диплинком чем тишина.
        fallback = self._template_fallback(language)
        logger.warning(f"LLM failed → template fallback: {fallback[:80]}")
        return fallback

    def is_relevant(self, message: str, keywords: list = None) -> bool:
        """
        Проверяет релевантность сообщения для обработки.

        Args:
            message: Текст сообщения
            keywords: Список ключевых слов для фильтрации

        Returns:
            True если сообщение релевантно
        """
        analysis = self.analyze_message(message)

        if analysis["interest_level"] > 0.1:
            return True

        if keywords:
            msg_lower = message.lower()
            return any(kw.lower() in msg_lower for kw in keywords)

        return False

    def health_check(self) -> bool:
        """Проверяет доступность OpenRouter API. Пробует все модели по очереди."""
        if not self.api_key:
            logger.error("API ключ не установлен")
            return False

        # Используем _make_request, который автоматически переключает модели при 429
        result = self._make_request(
            [{"role": "user", "content": "Hi"}],
            temperature=0.5
        )

        is_ok = result is not None
        if is_ok:
            logger.info(f"✅ OpenRouter API is healthy (active model: {self.model})")
        else:
            logger.error("❌ OpenRouter API check failed - all models hit rate limits")

        return is_ok
