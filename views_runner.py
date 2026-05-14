"""
Ручной запуск накрутки просмотров.
Используется dashboard.py через subprocess: python -m views_runner --group-id N --count 20 [--agents 1,2] [--msg-ids 100,101,102].
"""
import argparse
import asyncio
import logging
import sys

from pyrogram import Client

from agent_database import AgentDatabase
from config_agent import TELEGRAM_API_ID, TELEGRAM_API_HASH, SESSIONS_DIR
from views_module import ViewsManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("views_runner")


def _parse_int_list(s: str | None):
    if not s:
        return []
    return [int(x) for x in s.split(",") if x.strip().isdigit()]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-id", type=int, required=True, help="target_groups.id")
    ap.add_argument("--count", type=int, default=20, help="последних N постов если msg-ids не задан")
    ap.add_argument("--msg-ids", type=str, default="", help="конкретные id через запятую")
    ap.add_argument("--agents", type=str, default="", help="agent_accounts.id через запятую (по умолчанию все active)")
    args = ap.parse_args()

    db = AgentDatabase()

    # достаём группу прямым SQL — нет универсального getter'а по id
    conn = db.get_connection(); cur = conn.cursor()
    cur.execute("SELECT id, telegram_group_id, title, username, status FROM target_groups WHERE id=?", (args.group_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        logger.error(f"group {args.group_id} not found"); return 1
    grp = {'id': row[0], 'telegram_group_id': row[1], 'title': row[2], 'username': row[3], 'status': row[4]}

    chat_ref = grp.get('username') or grp.get('telegram_group_id') or grp.get('title')
    if not chat_ref:
        logger.error(f"group {args.group_id} has no resolvable ref"); return 1

    agent_ids = _parse_int_list(args.agents)
    if not agent_ids:
        conn = db.get_connection(); cur = conn.cursor()
        cur.execute("SELECT id FROM agent_accounts WHERE status IN ('active','healthy','online')")
        agent_ids = [r[0] for r in cur.fetchall()]
        conn.close()
    if not agent_ids:
        logger.error("no agents to use"); return 1

    msg_ids = _parse_int_list(args.msg_ids)

    logger.info(f"manual views: group={chat_ref} count={args.count} agents={agent_ids} msg_ids={msg_ids or 'last-N'}")

    total = 0
    for aid in agent_ids:
        agent = db.get_agent_account(aid)
        if not agent:
            logger.warning(f"agent {aid} not found, skip")
            continue

        session_name = agent.get('session_name') or f"agent_{aid}"
        kwargs = dict(name=session_name, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, workdir=SESSIONS_DIR)
        # proxy если есть
        try:
            from multi_agent import _parse_proxy_url
            pd = _parse_proxy_url(agent.get('proxy_url'))
            if pd:
                kwargs['proxy'] = pd
        except Exception:
            pass

        try:
            async with Client(**kwargs) as client:
                mgr = ViewsManager(aid, client, db, log_prefix=f"[agent {aid}]")
                if msg_ids:
                    n = await mgr.view_messages(chat_ref, msg_ids, group_db_id=args.group_id)
                else:
                    n = await mgr.view_last_n(chat_ref, n=args.count, group_db_id=args.group_id)
                logger.info(f"agent {aid}: +{n} views")
                total += n
        except Exception as e:
            logger.error(f"agent {aid} failed: {e}")

    logger.info(f"DONE: total +{total} views across {len(agent_ids)} agents")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
