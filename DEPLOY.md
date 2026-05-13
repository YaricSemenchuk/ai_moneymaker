# Деплой Telegram Mini App на Railway

## 1. Что было сделано

- `templates/base.html` — полностью переписан под брендовый тёмный дизайн с нижним таб-баром и подключением Telegram WebApp SDK. Через CSS-override переодевает все 6 страниц без правок их HTML.
- `dashboard.py` — добавлен `before_request` хук, который проверяет Telegram `initData` (HMAC по `BOT_TOKEN`) и пускает только user_id из whitelist.
- `Procfile`, `runtime.txt`, `railway.json`, обновлён `requirements.txt` (добавлен `gunicorn`).

## 2. Переменные окружения в Railway

Открой проект → **Variables**:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | Токен бота из @BotFather (обязательно) |
| `ALLOWED_USER_IDS` | Твой Telegram user_id, через запятую если несколько: `123456789,987654321` |
| `TELEGRAM_API_ID` | API ID (my.telegram.org/apps) |
| `TELEGRAM_API_HASH` | API Hash |
| `OPENROUTER_API_KEY` | Ключ OpenRouter |
| `OPENROUTER_MODEL` | (опционально) модель, например `meta-llama/llama-3-8b-instruct:free` |
| `REFERRAL_BOT` | (опционально) `@your_referral_bot` |
| `DB_PATH` | (опционально) `/data/moneymaker_agent.db` если используешь Railway Volume |
| `AUTH_DISABLED` | `1` только для дебага, в проде НЕ ставить |

> **Безопасность.** Если `BOT_TOKEN` или `ALLOWED_USER_IDS` не заданы, сервер вернёт 503 — это защита от случайной публикации без auth.

## 3. База данных

SQLite на Railway без Volume будет обнуляться при каждом деплое. Варианты:
- **Railway Volume** — добавить в сервисе volume на `/data`, и установить `DB_PATH=/data/moneymaker_agent.db`. Простейший путь.
- **Перейти на Postgres** — Railway даёт его в один клик, но нужна миграция кода (сейчас всё на sqlite3).

## 4. Подключение к боту в @BotFather

После того как Railway даст URL вида `https://your-app.up.railway.app`:

1. Открой @BotFather → `/mybots` → выбери своего бота
2. **Bot Settings → Menu Button → Configure menu button**
3. Текст: `Открыть MoneyMaker`
4. URL: `https://your-app.up.railway.app/`

Или через команду `/newapp` создай Web App с тем же URL — тогда сможешь делиться прямой ссылкой `t.me/your_bot/appname`.

## 5. Как получить свой user_id

Напиши боту [@userinfobot](https://t.me/userinfobot) — он пришлёт твой ID.

## 6. Локальный тест без Telegram

```bash
export AUTH_DISABLED=1
python dashboard.py
```

Откроется на `http://localhost:5001` без проверки. **Не деплой так в прод.**

## 7. Деплой

```bash
git init
git add .
git commit -m "telegram mini app"
# подключи репо в Railway UI, или через CLI:
railway login
railway link
railway up
```

Railway сам подхватит `Procfile` и `requirements.txt`, поставит gunicorn и запустит на отданном `$PORT`.

## 8. Проверка

- Открой URL Railway в браузере → должно отдать 401 (без initData это правильно)
- Открой Mini App в Telegram через menu button → должен пустить и показать брендовый дашборд
