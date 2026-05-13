"""
➕ Добавление Telegram аккаунта в систему.

Режимы:

1) Интерактивный (через SMS-код):
    python add_account.py

2) Импорт из tdata (Telegram Desktop):
    python add_account.py --from-tdata /path/to/tdata [--passcode ХХХ] \\
                          [--proxy socks5://user:pass@host:port] \\
                          [--name agent_3]

⚠️ После импорта tdata НЕЛЬЗЯ использовать оригинальный Telegram Desktop
   с этой же папкой — Telegram пометит как двойной логин.
"""
import argparse
import asyncio
import os
import sys
from typing import Optional

from pyrogram import Client
from config_agent import TELEGRAM_API_ID, TELEGRAM_API_HASH, SESSIONS_DIR
from agent_database import AgentDatabase


def _existing_count(db: AgentDatabase) -> int:
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM agent_accounts")
    n = cur.fetchone()[0]
    conn.close()
    return n


def _save_to_db(db: AgentDatabase, phone: str, session_name: str,
                proxy_url: Optional[str] = None, device_model: Optional[str] = None,
                system_version: Optional[str] = None, app_version: Optional[str] = None) -> int:
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO agent_accounts (phone_number, session_name, status, proxy_url, '
            'device_model, system_version, app_version) VALUES (?, ?, "active", ?, ?, ?, ?)',
            (phone, session_name, proxy_url, device_model, system_version, app_version),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return -1
    finally:
        conn.close()


async def add_via_sms(proxy_url: Optional[str]):
    print("=" * 60)
    print("➕ ДОБАВЛЕНИЕ TG АККАУНТА (SMS-код)")
    print("=" * 60)

    db = AgentDatabase()
    count = _existing_count(db)

    phone = input("📱 Номер (+79991234567): ").strip()
    if not phone.startswith("+"):
        print("❌ Должен начинаться с +")
        return

    default = f"agent_{count + 1}"
    session_name = input(f"💾 session-name [{default}]: ").strip() or default

    os.makedirs(SESSIONS_DIR, exist_ok=True)

    kwargs = dict(
        name=session_name, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH,
        phone_number=phone, workdir=SESSIONS_DIR,
    )
    from multi_agent import _parse_proxy_url
    pd = _parse_proxy_url(proxy_url)
    if pd:
        kwargs["proxy"] = pd
        print(f"🌐 proxy: {pd['hostname']}:{pd['port']}")

    client = Client(**kwargs)
    try:
        await client.start()
        me = await client.get_me()
        print(f"✅ Logged in: {me.first_name} (id={me.id})")
        agent_id = _save_to_db(db, phone, session_name, proxy_url=proxy_url)
        print(f"✅ В БД с id={agent_id}" if agent_id > 0 else "⚠️ Уже был в БД")
        await client.stop()
    except Exception as e:
        print(f"❌ {e}")


async def add_via_tdata(tdata_path: str, passcode: Optional[str],
                        proxy_url: Optional[str], session_name: Optional[str]):
    print("=" * 60)
    print("➕ ИМПОРТ TG АККАУНТА ИЗ TDATA")
    print("=" * 60)

    if not os.path.isdir(tdata_path):
        print(f"❌ Папка {tdata_path} не найдена")
        return

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        print("❌ opentele не установлен. Запусти: pip install opentele")
        return

    db = AgentDatabase()
    count = _existing_count(db)
    session_name = session_name or f"agent_{count + 1}"
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_path = os.path.join(SESSIONS_DIR, session_name)

    print(f"📂 tdata: {tdata_path}")
    print(f"💾 session: sessions/{session_name}.session")

    # Открываем tdata
    if passcode:
        tdesk = TDesktop(tdata_path, passcode=passcode)
    else:
        tdesk = TDesktop(tdata_path)

    if not tdesk.isLoaded():
        print("❌ tdata не загружен (неверный passcode?)")
        return

    # Конвертируем в Pyrogram session, сохраняя device fingerprint
    client: Client = await tdesk.ToPyrogram(
        session=session_path,
        flag=UseCurrentSession,
        api=None,  # использовать api_id/api_hash из tdata
    )

    from multi_agent import _parse_proxy_url
    pd = _parse_proxy_url(proxy_url)
    if pd:
        client.proxy = pd

    try:
        await client.start()
        me = await client.get_me()
        print(f"✅ Logged in: {me.first_name} {me.last_name or ''} (@{me.username or '-'}) id={me.id}")
        phone = (me.phone_number and f"+{me.phone_number}") or f"tdata_{me.id}"

        # Извлекаем device info из tdata если возможно
        device_model = getattr(client, "device_model", None)
        system_version = getattr(client, "system_version", None)
        app_version = getattr(client, "app_version", None)

        agent_id = _save_to_db(
            db, phone, session_name,
            proxy_url=proxy_url,
            device_model=device_model,
            system_version=system_version,
            app_version=app_version,
        )
        if agent_id > 0:
            print(f"✅ В БД с id={agent_id}")
        else:
            print("⚠️ Номер/session уже в БД (или конфликт)")
        await client.stop()
    except Exception as e:
        print(f"❌ {e}")
        try:
            await client.stop()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-tdata", dest="tdata", help="Путь к папке tdata")
    parser.add_argument("--passcode", help="Local passcode для tdata (если защищён)")
    parser.add_argument("--proxy", help="socks5://user:pass@host:port")
    parser.add_argument("--name", help="session_name (по умолчанию agent_N)")
    args = parser.parse_args()

    if not TELEGRAM_API_ID or TELEGRAM_API_ID == 1234567:
        print("❌ TELEGRAM_API_ID не настроен в .env")
        sys.exit(1)

    if args.tdata:
        asyncio.run(add_via_tdata(args.tdata, args.passcode, args.proxy, args.name))
    else:
        asyncio.run(add_via_sms(args.proxy))


if __name__ == "__main__":
    main()
