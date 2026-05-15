import os
from dotenv import load_dotenv

load_dotenv()

# Telegram API
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "1234567"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "YOUR_API_HASH")

# OpenRouter API (бесплатные модели)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-2-7b-chat")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Реферальная ссылка на платформу с заданиями (по умолчанию)
REFERRAL_BOT = os.getenv("REFERRAL_BOT", "@moneymakerquest_bot")

# Цели для разных аккаунтов (один акк → бот, другой → группа)
# Описания нужны чтобы LLM понимал контекст что предлагать
REFERRAL_TARGETS = {
    "@moneymakerquest_bot": {
        "type": "bot",
        "description": "Telegram-бот с заданиями за USDT. Подходит для людей которые хотят сразу зарабатывать.",
        "call_to_action": "Напиши боту {target} — там простые задания с оплатой.",
    },
    "@moneymaker_app": {
        "type": "group",
        "description": "Telegram-группа с обсуждением заработка, новостями платформы, опытом пользователей.",
        "call_to_action": "Заходи в группу {target} — там обсуждают разные способы заработка.",
    },
}

# Распределение по агентам (если в БД не указано иное)
# Agent #1 → бот, Agent #2 → группа, остальные чередуются
DEFAULT_AGENT_REFERRALS = {
    1: "@moneymakerquest_bot",
    2: "@moneymaker_app",
}

# Приоритетные группы - агент СРАЗУ в них вступит (свои каналы, важные группы)
PRIORITY_GROUPS = [
    "@moneymaker_quest",  # Своя группа проекта
]

# Целевые ключевые слова - используются для реактивных ответов
TARGET_KEYWORDS = [
    "заработок", "заработать", "доход", "подработка", "халтура",
    "работа онлайн", "удаленка", "фриланс", "вакансии",
    "криптовалюта", "крипта", "крипто", "заработок в интернете",
    "earn money", "make money", "side hustle", "remote work",
]

# Эмоциональные/болевые триггеры - на них реагируем агрессивнее (interest +0.4)
# Это люди в моменте принятия решения, конверсия выше в разы.
EMOTIONAL_TRIGGERS_RU = [
    "нет денег", "нужны деньги", "срочно деньги", "хочу заработать",
    "не хватает", "копейки", "копейки платят", "мизер", "гроши",
    "без работы", "потерял работу", "уволили", "сократили",
    "помогите советом", "куда податься", "что делать", "накипело",
    "ищу варианты", "ищу способ", "посоветуйте", "подскажите как",
    "плачу за", "выплат", "вывел", "вывод", "вытащил",
]
EMOTIONAL_TRIGGERS_EN = [
    "no money", "broke", "need money", "lost my job", "fired",
    "any advice", "what should i do", "looking for ways",
    "cant find work", "can't find work", "any tips",
    "paid out", "withdraw", "cashout",
]

# Большой пул слов для авто-парсинга (выбираются случайно по 5-8 за раз)
SCOUT_KEYWORD_POOL = {
    "money_general": [
        "заработок", "заработать", "доход", "пассивный доход",
        "деньги", "финансы", "монетизация", "профит", "прибыль",
    ],
    "side_jobs": [
        "подработка", "работа онлайн", "удаленка", "удаленная работа",
        "вакансии", "халтура", "работа без опыта", "работа из дома",
        "приработок", "доп заработок", "поиск работы",
    ],
    "freelance": [
        "фриланс", "freelance", "заказы", "проекты онлайн",
        "копирайтинг", "дизайн заказы", "веб-разработка заказы",
    ],
    "crypto": [
        "криптовалюта", "крипта", "крипто", "crypto", "bitcoin",
        "майнинг", "трейдинг", "инвестиции крипто", "airdrop",
        "криптообмен", "p2p обмен", "стейкинг",
    ],
    "students": [
        "студенты заработок", "школьники подработка",
        "ГДЗ", "карманные деньги", "первый заработок", "до 18",
    ],
    "moms": [
        "мамы в декрете", "декретный отпуск", "мамочки заработок",
        "мама онлайн", "работа в декрете",
    ],
    "regions_ru": [
        "Москва работа", "СПб работа", "регионы заработок",
        "Екатеринбург подработка", "Казань работа", "Новосибирск работа",
    ],
    "regions_ua": [
        "Київ робота", "Україна заробіток", "Дніпро робота",
        "Львів робота", "Одеса робота", "Харків робота",
    ],
    "english": [
        "earn money", "make money", "side hustle", "remote work",
        "online job", "passive income", "freelance work", "make money online",
        "earn online", "extra income", "work from home",
    ],
    "broke_searching": [
        "нет денег", "нужны деньги", "срочно деньги",
        "хочу заработать", "не хватает денег", "ищу варианты",
    ],
}

# Часовой пояс отображения (только для подписей в UI/логах)
# datetime.now() использует системный часовой пояс - сервер в Украине = Kyiv time
TIMEZONE_LABEL = "Киев"  # для UI: "11:00 Киев" вместо "11:00 МСК"

# Настройки автоматического парсинга
AUTO_SCOUT_ENABLED = True
AUTO_SCOUT_TIMES = [11, 18]  # часы по Киеву когда запускать (2 раза в день)
AUTO_SCOUT_KEYWORDS_PER_RUN = 6  # сколько случайных слов брать за один прогон
AUTO_SCOUT_DELAY_MIN = 30  # минимальная пауза между словами (сек)
AUTO_SCOUT_DELAY_MAX = 60  # максимальная пауза (сек)
AUTO_SCOUT_TIME_JITTER = 1800  # ±30 мин случайности к запланированному времени
# Сколько случайных агентов в день назначается на парсинг (детерминированно
# по дате — все агенты независимо приходят к одному набору).
AUTO_SCOUT_AGENTS_PER_DAY = 2

# Системный промпт для агента
AGENT_PROMPT_TEMPLATE = """
Ты дружелюбный ассистент, который помогает людям найти возможности для заработка.
Твоя задача - ненавязчиво предлагать платформу {referral_bot}, когда кто-то ищет подработку или способы заработать.
Избегай прямого спама и агрессивных продаж. Отвечай естественно, как обычный пользователь, который делится полезной информацией.
Если тебя спрашивают о деталях, направляй на платформу, объясняя, что там можно найти простые задания с оплатой.
Отвечай коротко (2-3 предложения максимум).
"""

# Антибан-защита
MESSAGE_DELAY_MIN = 20               # было 60 — задержка между сообщениями
MESSAGE_DELAY_MAX = 90               # было 300
MAX_MESSAGES_PER_HOUR = 15           # было 8 — больше hot-replies в час
MAX_GROUPS_TO_JOIN_PER_DAY = 12
MIN_GROUP_MEMBERS = 150              # было 500 — даём шанс микро-сообществам с высоким интентом
MAX_GROUP_MEMBERS_FOR_PROACTIVE = 100000  # слишком крупные = низкий CTR, посты тонут

# Реактив (listening) — пороги
LISTENING_INTEREST_THRESHOLD = 0.12   # было 0.20 — ловим эмоциональные триггеры и одиночные keywords
LISTENING_EMOTIONAL_BONUS = 0.40      # +к interest если есть emotional trigger
REPLY_VIA_DM = True                   # отвечать в личку + хук в группе
DM_FALLBACK_TO_GROUP = True           # если DM запрещён — слать в группу
REPLY_GROUP_INCLUDES_LINK = True      # хук в группе содержит диплинк (главный рычаг трафика).
                                      # Выкл если рост банов >3% → False

# Авто-блэклист групп по фейлам
AUTO_DISABLE_AFTER_FAILS = 3         # N подряд фейлов → no_permission

# Опасные ниши — мгновенный бан / детская аудитория / строгая модерация.
# Если такое слово найдено в названии группы → НЕ вступаем и НЕ отвечаем.
GROUP_TITLE_BLACKLIST = [
    # игровые / детские — там бан моментальный, аудитория не наша
    "майнкрафт", "minecraft", "роблокс", "roblox", "fortnite", "фортнайт",
    "брейнрот", "brainrot", "школьник", "школьн", "school", "детск", "kids",
    "child", "podrostk", "подростк", "юниор", "junior",
    "аниме чат", "anime chat", "anime", "манга", "manga",
    # юр/политика — там не наша аудитория и строгие модераторы
    "политик", "polit", "новости", "news only",
    # NSFW
    "18+", "nsfw", "adult only",
    # работа/вакансии — высокий риск спам-репортов от соискателей
    "вакансі", "вакансии", "работа ", "работа/", "job ", "jobs ", "hiring",
    "подработк", "робота ", "робота/",
    # финансовые/обменники/P2P — там любая сторонняя промо-ссылка = автобан
    "обмен валют", "обмін валют", "обменник", "обмінник", "обмен крипт",
    "p2p", "p-2-p", "exchange", "wechange", "swap ", "usdt обмен",
    "обмен usdt", "currency exchange", "btc обмен", "крипто обмен",
    "форекс", "forex", "трейдинг", "trading chat",
]

# При SLOWMODE_WAIT — пропустить группу на (wait_sec * множитель) сек.
SLOWMODE_BACKOFF_MULTIPLIER = 2.0

# Сколько секунд минимум ждать после SLOWMODE прежде чем ВООБЩЕ снова трогать
# эту группу любым агентом. Записывается в memory cache процесса.
SLOWMODE_MIN_COOLDOWN_SEC = 1800     # 30 мин

# AT_RISK — что считать "баном". Сюда НЕ входят SLOWMODE/FORBIDDEN/PRIVATE —
# это не наша вина, а закрытые двери. Считаются только реальные баны акка.
AT_RISK_BAN_ERROR_CODES = ["USER_BANNED", "BANNED_RIGHTS", "PEER_FLOOD"]
AT_RISK_THRESHOLD_24H = 5            # 3→5: чтобы не отрубать агента из-за slowmode

# Доп. защита: суммарно фейлов любого типа за 24ч → агент в риске.
# Ловит "пакетные" сбои (как A#1/A#2 с 75 фейлов/неделя) до того как пойдут реальные баны.
AT_RISK_FAIL_THRESHOLD_24H = 20

# Глобальный лимит ВСЕХ действий (proactive + reply) на агента в сутки.
# Защита от поведенческих банов: чем больше постов в день, тем выше
# шанс что админ хоть одной группы заметит и репортнет.
MAX_TOTAL_ACTIONS_PER_DAY = 60       # было 30 — расширяем под bigger funnel

# Проактивный режим (агент сам пишет первым)
PROACTIVE_ENABLED = True
PROACTIVE_MAX_POSTS_PER_DAY = 20     # было 6 — 97% групп раньше не получали ничего
PROACTIVE_MIN_INTERVAL_HOURS = 0.5   # было 1.5 — между постами не часами а получасом
PROACTIVE_MAX_INTERVAL_HOURS = 2     # было 4
PROACTIVE_GROUP_COOLDOWN_DAYS = 2    # 4→2 для здоровых групп
# Лимит постов от одного агента в одну группу за последние 24 часа.
# Жёстче чем COOLDOWN_DAYS: даже если cooldown снят руками, эта проверка держит.
PROACTIVE_MAX_POSTS_PER_GROUP_PER_DAY = 2

# Реакции на сообщения активных юзеров (мягкое вовлечение).
REACTIONS_ENABLED = True               # глобальный выключатель
REACTION_EMOJIS = ["👍", "❤️"]          # из чего выбираем случайно
REACTION_INTEREST_MIN = 0.10           # ниже — не реагируем
REACTION_INTEREST_MAX = 0.20           # выше — обычно идёт ответ, реакция не нужна
REACTIONS_MAX_PER_HOUR = 8             # антифлуд на агента
PROACTIVE_ACTIVE_HOUR_START = 0      # было 10 — покрываем все таймзоны
PROACTIVE_ACTIVE_HOUR_END = 24       # было 22

# Логирование
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
DB_PATH = os.getenv("DB_PATH", "moneymaker_agent.db")
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")

# === Админ-бот для алертов и отчётов ===
# Создай бота через @BotFather, токен в Railway env.
# ADMIN_CHAT_ID = твой личный telegram_id (узнать через @userinfobot).
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")    # токен @aismoneymaker_bot
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")        # куда слать алерты (твой chat_id)
ADMIN_ALERTS_ENABLED = bool(ADMIN_BOT_TOKEN and ADMIN_CHAT_ID)
# Минимум секунд между одинаковыми алертами на одну группу — анти-флуд.
ADMIN_BAN_ALERT_COOLDOWN_SEC = 600

# === Трекинг конверсий ===
# Секрет для HMAC-подписи POST /api/track-signup (бот → дашборд).
# Тот же секрет должен быть в env @moneymakerquest_bot.
TRACK_SECRET = os.getenv("TRACK_SECRET", "")
# Публичный URL дашборда (для @moneymakerquest_bot, чтобы он знал куда стучать).
# На Railway это что-то вроде https://moneymaker-agent.up.railway.app
DASHBOARD_PUBLIC_URL = os.getenv("DASHBOARD_PUBLIC_URL", "")
