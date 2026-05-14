"""
Ручная накрутка лайков (реакций) на последние посты в группе/канале.
Запускается из dashboard.py через subprocess:
  python -m reactions_runner --group-id N --count 10 [--agents 1,2] [--emoji 👍] [--msg-ids 100,101]
"""
import argparse
import asyncio
import logging
import os
import random
import sys
import time

from pyrogram import Client

from agent_database import AgentDatabase
from config_agent import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, SESSIONS_DIR,
    REACTION_EMOJIS, REACTIONS_MAX_PER_HOUR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reactions_runner")

# Безопасные паузы — реакции видны другим юзерам, нельзя слать пачкой
JITTER_MIN = float(os.getenv("REACTIONS_JITTER_MIN_SEC", "5"))
JITTER_MAX = float(os.getenv("REACTIONS_JITTER_MAX_SEC", "20"))


def _parse_int_list(s: str | None):
    if not s:
        return []
    return [int(x) for x in s.split(",") if x.strip().lstrip('-').isdigit()]


async def react_with_client(client, db, agent_id, chat_ref, message_ids, group_db_id, emoji_pool):
    """Реагирует на message_ids от лица одного клиента. Уважает REACTIONS_MAX_PER_HOUR."""
    sent = 0
    ts = []
    for mid in message_ids:
        # rate-limit
        now = time.time()
        ts = [t for t in ts if now - t < 3600]
        if len(ts) >= REACTIONS_MAX_PER_HOUR:
            logger.info(f"[agent {agent_id}] reactions hourly limit reached, stop")
            break

        emoji = random.choice(emoji_pool)
        try:
            await client.send_reaction(chat_ref, mid, emoji)
            sent += 1
            ts.append(time.time())
            logger.info(f"[agent {agent_id}] reacted {emoji} on msg {mid} in {chat_ref}")
            # лог в interactions (как одна запись на каждую реакцию)
            try:
                conn = db.get_connection(); cur = conn.cursor()
                cur.execute(
                    "INSERT INTO interactions (agent_id, group_id, interaction_type, status, response_text) "
                    "VALUES (?, ?, 'reaction', 'ok', ?)",
                    (agent_id, group_db_id, f"{emoji} msg {mid}")
                )
                conn.commit(); conn.close()
            except Exception as e:
                logger.warning(f"can't log reaction: {e}")
        except Exception as e:
            name = type(e).__name__
            logger.warning(f"[agent {agent_id}] reaction {emoji} on {mid} failed ({name}): {e}")
            if 'FloodWait' in name:
                wait = getattr(e, 'value', 30)
                logger.warning(f"[agent {agent_id}] FloodWait {wait}s, stop this run")
                break
            # ReactionInvalid / ChatRestricted / etc. — пропускаем дальше
        await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
    return sent


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group-id", type=int, required=True)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--msg-ids", type=str, default="")
    ap.add_argument("--agents", type=str, default="")
    ap.add_argument("--emoji", type=str, default="", help="конкретный эмодзи, по умолчанию случайный из пула")
    args = ap.parse_args()

    db = AgentDatabase()

    conn = db.get_connection(); cur = conn.cursor()
    cur.execute("SELECT id, telegram_group_id, title, username, status FROM target_groups WHERE id=?", (args.group_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        logger.error(f"group {args.group_id} not found"); return 1
    chat_ref = row[3] or row[1] or row[2]
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
    emoji_pool = [args.emoji] if args.emoji else REACTION_EMOJIS

    logger.info(f"manual reactions: group={chat_ref} count={args.count} agents={agent_ids} emoji_pool={emoji_pool}")

    total = 0
    for aid in agent_ids:
        agent = db.get_agent_account(aid)
        if not agent:
            logger.warning(f"agent {aid} not found, skip"); continue

        session_name = agent.get('session_name') or f"agent_{aid}"
        kwargs = dict(name=session_name, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH, workdir=SESSIONS_DIR)
        try:
            from multi_agent import _parse_proxy_url
            pd = _parse_proxy_url(agent.get('proxy_url'))
            if pd:
                kwargs['proxy'] = pd
        except Exception:
            pass

        try:
            async with Client(**kwargs) as client:
                # если msg_ids не задан — берём последние args.count постов
                ids = msg_ids
                if not ids:
                    ids = []
                    async for msg in client.get_chat_history(chat_ref, limit=args.count):
                        ids.append(msg.id)
                n = await react_with_client(client, db, aid, chat_ref, ids, args.group_id, emoji_pool)
                logger.info(f"agent {aid}: +{n} reactions")
                total += n
        except Exception as e:
            logger.error(f"agent {aid} failed: {e}")

    logger.info(f"DONE: total +{total} reactions across {len(agent_ids)} agents")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
