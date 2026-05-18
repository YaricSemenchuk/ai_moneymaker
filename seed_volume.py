"""Одноразовый сидер volume `/data` на Railway.

Session-файлы Telegram-аккаунтов и БД нельзя держать в публичном git-репо.
Поэтому при первом старте бот скачивает архив с ними по приватной ссылке
(env SEED_URL) и распаковывает в каталог volume.

Идемпотентно: если в SESSIONS_DIR уже есть *.session — ничего не делает.
После первого успешного деплоя SEED_URL можно убрать из env.

Архив должен быть .tar.gz со структурой:
    sessions/<account>.session   (каталог сессий)
    moneymaker_agent.db          (файл БД)
Создать локально:  tar czf seed.tgz sessions moneymaker_agent.db
"""
import os
import sys
import glob
import tarfile
import tempfile
import urllib.request

SEED_URL = os.getenv("SEED_URL", "").strip()
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
# Каталог volume, куда распаковываем (родитель sessions/ и БД).
SEED_TARGET = os.getenv("SEED_TARGET", os.path.dirname(SESSIONS_DIR.rstrip("/")) or "/data")


def already_seeded() -> bool:
    return bool(glob.glob(os.path.join(SESSIONS_DIR, "*.session")))


def main() -> None:
    if already_seeded():
        print(f"[seed_volume] {SESSIONS_DIR} уже содержит сессии — пропускаю")
        return
    if not SEED_URL:
        print("[seed_volume] SEED_URL не задан и сессий нет — бот не сможет залогиниться", file=sys.stderr)
        return

    print(f"[seed_volume] качаю seed-архив из SEED_URL → {SEED_TARGET}")
    os.makedirs(SEED_TARGET, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            with urllib.request.urlopen(SEED_URL, timeout=120) as resp:
                tmp.write(resp.read())
            archive_path = tmp.name
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(SEED_TARGET)
        os.unlink(archive_path)
        print(f"[seed_volume] готово: распаковано в {SEED_TARGET}")
    except Exception as e:
        print(f"[seed_volume] ОШИБКА сидинга: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
