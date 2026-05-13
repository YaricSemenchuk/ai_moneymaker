# Перенос БД и сессий на Railway

`.session` файлы Telegram — это **полные credentials аккаунта**, обходят 2FA. Их нельзя класть в git ни в каком виде. То же про `moneymaker_agent.db` — там персональные данные спарсенных юзеров.

Поэтому: заливаем напрямую в Railway Volume через CLI, минуя GitHub.

## 1. Подготовка на Railway (UI)

1. New Project → Deploy from GitHub repo → `ai_moneymaker`
2. После первого билда: **Settings → Volumes → New Volume**
   - Mount path: `/data`
   - Size: 1 GB (с запасом)
3. **Variables** — выставь все из [DEPLOY.md](DEPLOY.md), и обязательно:
   ```
   DB_PATH=/data/moneymaker_agent.db
   SESSIONS_DIR=/data/sessions
   ```
4. Дождись первого деплоя (он упадёт если БД нет — это норма, сейчас зальём).

## 2. Установить Railway CLI локально

```bash
brew install railway   # или: npm i -g @railway/cli
railway login
cd /Volumes/secondary/bot
railway link           # выбери свой проект и service
```

## 3. Залить БД и сессии в Volume

Railway даёт `railway ssh` (shell внутрь контейнера) и `railway run` (выполнить локальную команду в среде проекта). Самый надёжный способ — base64-перегон через ssh:

```bash
# БД
base64 -i moneymaker_agent.db | railway ssh "base64 -d > /data/moneymaker_agent.db"

# Папка с сессиями — tar.gz через pipe
tar czf - sessions/ | railway ssh "mkdir -p /data && tar xzf - -C /data && mv /data/sessions /data/sessions.new || true && ls /data"
```

Альтернатива, если `railway ssh` недоступен в твоём плане — добавь временный protected upload-endpoint (могу написать) или используй `rsync` через [`ngrok tcp 22`](https://ngrok.com) → SSH в контейнер.

## 4. Проверить

```bash
railway ssh "ls -lh /data/ /data/sessions/"
```

Должно показать `moneymaker_agent.db` и список `.session` файлов.

## 5. Передеплоить

В Railway UI → Deployments → Redeploy. Теперь Flask стартует с готовой БД, агенты — со своими сессиями.

## 6. Безопасность

- **Не запускай локально и на Railway одновременно** один и тот же session. Telegram увидит логин с двух IP, могут ливнуть AuthKeyDuplicated → потеря сессии и риск временного бана.
- Перед заливкой на Railway — **останови локального бота** (`pkill -f multi_agent` или `pkill -f main.py`), чтобы локальная копия не пересоздала session-журнал во время заливки.
- После успешного деплоя удали `sessions/` локально или заархивируй: эти файлы = доступ к аккаунтам.
