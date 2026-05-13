"""
🧪 Скрипт для тестирования проактивного модуля.
Запускает один проактивный пост ВРУЧНУЮ, минуя задержки и лимиты.

Использование:
    python test_proactive.py           # один пост
    python test_proactive.py --dry     # сгенерировать и показать БЕЗ отправки
"""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main(dry_run: bool = False):
    from pyrogram import Client
    from config_agent import TELEGRAM_API_ID, TELEGRAM_API_HASH, DB_PATH
    from agent_database import AgentDatabase
    from llm_analyzer import LLMAnalyzer
    from proactive_module import ProactiveModule

    print("=" * 60)
    print("🧪 ТЕСТ ПРОАКТИВНОГО МОДУЛЯ")
    print("=" * 60)

    db = AgentDatabase(DB_PATH)
    llm = LLMAnalyzer()

    # Проверяем что есть группы
    groups = db.get_target_groups(status="joined", limit=10)
    print(f"\n📋 В БД групп со статусом 'joined': {len(groups)}")

    if not groups:
        print("❌ Нет групп для постинга! Сначала запусти main.py чтобы агент вступил в группы.")
        return

    print("\nГруппы где агент состоит:")
    for g in groups[:5]:
        print(f"  • {g['title']} (@{g.get('username', 'нет')})")

    # Подключаемся к Telegram
    client = Client(
        name="agent_session_1",
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH
    )

    print("\n🔌 Подключаюсь к Telegram...")
    await client.start()
    me = await client.get_me()
    print(f"✅ Logged in as: {me.first_name}")

    # Создаём модуль
    proactive = ProactiveModule(client, db, llm, agent_id=1)

    if dry_run:
        # Только генерация, без отправки
        print("\n🎯 DRY RUN: генерирую пост БЕЗ отправки")

        group = proactive.select_target_group()
        if not group:
            print("❌ Нет подходящих групп (все в cooldown)")
            await client.stop()
            return

        title = group.get("title", "")
        has_cyrillic = any('А' <= c <= 'я' for c in title)
        language = "ru" if has_cyrillic else "en"

        print(f"📍 Группа: {title}")
        print(f"🌐 Язык: {language}")

        post = await proactive.generate_post(language=language)
        if post:
            print(f"\n📝 Шаблон: {post['template']}")
            print(f"💬 Текст:\n   {post['text']}")
        else:
            print("❌ LLM не вернул текст")

    else:
        # Реальная отправка с обходом всех лимитов
        print("\n🚀 Отправляю проактивный пост (минуя все лимиты)...")

        # Временно отключаем проверки времени
        proactive.is_active_hour = lambda: True
        proactive.can_post_today = lambda: True

        sent = await proactive.send_proactive_post()

        if sent:
            print("\n✅ ПОСТ ОТПРАВЛЕН!")
            print("Проверь:")
            print("  • Свой Telegram аккаунт — увидишь сообщение в группе")
            print("  • Дашборд http://localhost:5001/proactive")
        else:
            print("\n❌ Не удалось отправить пост")
            print("Проверь логи выше для деталей")

    await client.stop()
    print("\n" + "=" * 60)


if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    asyncio.run(main(dry_run=dry_run))
