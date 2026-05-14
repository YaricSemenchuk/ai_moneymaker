"""
🎯 Dashboard для MoneyMaker AI-Agent
Веб-интерфейс для просмотра статистики, групп и истории сообщений.
Также работает как Telegram Mini App (см. tg_auth ниже).
"""
import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qsl
try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # pragma: no cover — fallback for sparse envs
    KYIV_TZ = timezone(timedelta(hours=3))
from flask import Flask, render_template, jsonify, request, abort
from config_agent import DB_PATH


# ============================================
# TELEGRAM MINI APP AUTH
# ============================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
}
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0") == "1"


def _verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app.
    Returns parsed dict on success, None on failure."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if "user" in pairs:
            try:
                pairs["user"] = json.loads(pairs["user"])
            except Exception:
                pass
        return pairs
    except Exception:
        return None


def tg_auth(f):
    """Decorator: requires valid Telegram initData with user_id in ALLOWED_USER_IDS."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if AUTH_DISABLED:
            return f(*args, **kwargs)
        if not BOT_TOKEN or not ALLOWED_USER_IDS:
            # safety: if not configured, block everything
            abort(503, "Auth not configured: set BOT_TOKEN and ALLOWED_USER_IDS")
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        # For HTML page loads (no header), let it through — JS will then attach the header for API calls.
        if not init_data and request.path in {"/", "/groups", "/messages", "/users", "/proactive", "/agents"}:
            return f(*args, **kwargs)
        parsed = _verify_init_data(init_data)
        if not parsed:
            abort(401, "Invalid initData")
        user = parsed.get("user") or {}
        if int(user.get("id", 0)) not in ALLOWED_USER_IDS:
            abort(403, "User not whitelisted")
        return f(*args, **kwargs)
    return wrapper


def _to_kyiv(ts: str):
    """Парсит SQLite-timestamp (UTC, naive) и возвращает datetime в Europe/Kyiv."""
    if not ts:
        return None
    s = ts.replace("T", " ").split(".")[0]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc).astimezone(KYIV_TZ)

app = Flask(__name__)


@app.before_request
def _enforce_tg_auth():
    """Apply Telegram Mini App auth to every request (except static)."""
    if AUTH_DISABLED:
        return None
    if request.endpoint == "static":
        return None
    if not BOT_TOKEN or not ALLOWED_USER_IDS:
        return ("Auth not configured: set BOT_TOKEN and ALLOWED_USER_IDS env vars", 503)

    init_data = request.headers.get("X-Telegram-Init-Data", "")

    # HTML page loads have no header (browser nav). Let the shell render — JS then attaches
    # X-Telegram-Init-Data to every subsequent fetch(), and API endpoints below enforce.
    is_api = request.path.startswith("/api/")
    if not init_data and not is_api:
        return None

    parsed = _verify_init_data(init_data)
    if not parsed:
        return ("Unauthorized: invalid Telegram initData", 401)
    user = parsed.get("user") or {}
    if int(user.get("id", 0)) not in ALLOWED_USER_IDS:
        return ("Forbidden: user not whitelisted", 403)
    return None


def get_db_connection():
    """Получает соединение с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================
# СТАТИСТИКА
# ============================================

@app.route('/')
def index():
    """Главная страница дашборда."""
    return render_template('index.html')


@app.route('/api/stats')
def api_stats():
    """API: общая статистика."""
    conn = get_db_connection()

    # Общее количество групп
    total_groups = conn.execute("SELECT COUNT(*) as c FROM target_groups").fetchone()["c"]

    # По статусам
    groups_by_status = conn.execute("""
        SELECT status, COUNT(*) as count
        FROM target_groups
        GROUP BY status
    """).fetchall()

    # Общее количество взаимодействий
    total_interactions = conn.execute("SELECT COUNT(*) as c FROM interactions").fetchone()["c"]

    # Сегодня (наблюдения + ответы — синхронно с графиком "Активность за 30 дней")
    today_interactions = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM interactions WHERE date(created_at) = date('now')) +
            (SELECT COUNT(*) FROM user_messages WHERE date(created_at) = date('now')) AS c
    """).fetchone()["c"]

    # За последние 7 дней
    week_interactions = conn.execute("""
        SELECT COUNT(*) as c FROM interactions
        WHERE created_at >= datetime('now', '-7 days')
    """).fetchone()["c"]

    # Уникальных пользователей
    unique_users = conn.execute("""
        SELECT COUNT(DISTINCT user_id) as c FROM interactions
        WHERE user_id IS NOT NULL
    """).fetchone()["c"]

    # Активных аккаунтов агентов
    active_agents = conn.execute("SELECT COUNT(*) as c FROM agent_accounts").fetchone()["c"]

    conn.close()

    return jsonify({
        "total_groups": total_groups,
        "total_interactions": total_interactions,
        "today_interactions": today_interactions,
        "week_interactions": week_interactions,
        "unique_users": unique_users,
        "active_agents": active_agents,
        "groups_by_status": {row["status"]: row["count"] for row in groups_by_status}
    })


@app.route('/api/activity-by-day')
def api_activity_by_day():
    """API: активность по дням за последние 30 дней."""
    conn = get_db_connection()

    out_sent = conn.execute("""
        SELECT created_at FROM interactions
        WHERE status='sent' AND created_at >= datetime('now', '-30 days')
    """).fetchall()
    out_failed = conn.execute("""
        SELECT created_at FROM interactions
        WHERE status='failed' AND created_at >= datetime('now', '-30 days')
    """).fetchall()
    inbound = conn.execute("""
        SELECT created_at FROM user_messages
        WHERE created_at >= datetime('now', '-30 days')
    """).fetchall()
    conn.close()

    def bucket(rows):
        b: dict = {}
        for r in rows:
            dt = _to_kyiv(r["created_at"])
            if not dt:
                continue
            k = dt.strftime("%Y-%m-%d")
            b[k] = b.get(k, 0) + 1
        return b

    sent_b = bucket(out_sent)
    failed_b = bucket(out_failed)
    inbound_b = bucket(inbound)

    all_days = sorted(set(sent_b) | set(failed_b) | set(inbound_b))
    return jsonify({
        "labels": all_days,
        "sent": [sent_b.get(d, 0) for d in all_days],
        "failed": [failed_b.get(d, 0) for d in all_days],
        "inbound": [inbound_b.get(d, 0) for d in all_days],
        # для обратной совместимости — общая сумма
        "data": [sent_b.get(d, 0) + failed_b.get(d, 0) + inbound_b.get(d, 0) for d in all_days],
    })


@app.route('/api/activity-by-hour')
def api_activity_by_hour():
    """API: активность по часам, стэк по дням, время — Europe/Kyiv.

    Возвращает:
        labels: ["00", "01", ..., "23"]
        datasets: [{label: "2026-05-09", data: [c0, c1, ..., c23]}, ...]
    """
    # from/to — даты в формате YYYY-MM-DD по Europe/Kyiv (включительно).
    # Если не переданы — по умолчанию только сегодня.
    today_kyiv = datetime.now(tz=KYIV_TZ).strftime("%Y-%m-%d")
    date_from = request.args.get("from") or today_kyiv
    date_to = request.args.get("to") or today_kyiv

    # Конвертим киевскую полночь в UTC для запроса к БД.
    try:
        from_dt_kyiv = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=KYIV_TZ)
        to_dt_kyiv = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=KYIV_TZ) + timedelta(days=1)
    except ValueError:
        return jsonify({"error": "bad date format, expected YYYY-MM-DD"}), 400

    from_utc = from_dt_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    to_utc = to_dt_kyiv.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT created_at, 'reply' AS kind FROM interactions
        WHERE created_at >= ? AND created_at < ?
        UNION ALL
        SELECT created_at, 'observed' AS kind FROM user_messages
        WHERE created_at >= ? AND created_at < ?
        UNION ALL
        SELECT created_at, 'proactive' AS kind FROM proactive_posts
        WHERE created_at >= ? AND created_at < ?
        """,
        (from_utc, to_utc, from_utc, to_utc, from_utc, to_utc),
    ).fetchall()
    conn.close()

    # buckets[YYYY-MM-DD][HH] = {"reply": n, "observed": n, "proactive": n}
    buckets: dict = {}
    for r in rows:
        dt = _to_kyiv(r["created_at"])
        if not dt:
            continue
        day = dt.strftime("%Y-%m-%d")
        h = int(dt.strftime("%H"))
        if day not in buckets:
            buckets[day] = [{"reply": 0, "observed": 0, "proactive": 0} for _ in range(24)]
        buckets[day][h][r["kind"]] += 1

    # Стабильные цвета (повторяются по кругу)
    palette = [
        "#764ba2", "#667eea", "#10b981", "#f59e0b", "#ef4444",
        "#06b6d4", "#ec4899", "#84cc16", "#8b5cf6", "#f97316",
    ]
    days_sorted = sorted(buckets.keys())
    datasets = [
        {
            "label": day,
            "data": [b["reply"] + b["observed"] + b["proactive"] for b in buckets[day]],
            "breakdown": buckets[day],  # массив из 24 объектов с детализацией
            "backgroundColor": palette[i % len(palette)],
        }
        for i, day in enumerate(days_sorted)
    ]

    return jsonify({
        "labels": [f"{h:02d}" for h in range(24)],
        "datasets": datasets,
        "timezone": "Europe/Kyiv",
        "from": date_from,
        "to": date_to,
    })


@app.route('/api/agents-risk')
def api_agents_risk():
    """API: для каждого агента — статус at-risk и время восстановления."""
    conn = get_db_connection()

    agents = conn.execute("""
        SELECT id, COALESCE(NULLIF(session_name, ''), phone_number, 'agent_' || id) as label, status
        FROM agent_accounts
        ORDER BY id
    """).fetchall()

    result = []
    for a in agents:
        rows = conn.execute("""
            SELECT last_attempt FROM agent_group_membership
            WHERE agent_id = ? AND status IN ('banned', 'failed')
              AND last_attempt >= datetime('now', '-24 hours')
            ORDER BY last_attempt ASC
        """, (a["id"],)).fetchall()

        bans = [r["last_attempt"] for r in rows]
        count = len(bans)
        at_risk = count >= 3

        recover_at = None
        if at_risk:
            # Чтобы счётчик упал до 2 (<3), должны истечь (count - 2) самых старых банов.
            # Граничный — индекс (count - 3) в отсортированном по возрастанию списке.
            threshold_ts = bans[count - 3]
            recover_at = threshold_ts  # это дата last_attempt; +24ч добавим на клиенте

        result.append({
            "id": a["id"],
            "label": a["label"],
            "status": a["status"],
            "bans_24h": count,
            "at_risk": at_risk,
            "recover_after": recover_at,  # ISO-строка last_attempt граничного бана; восстановление = +24ч
        })

    conn.close()
    return jsonify({"agents": result})



@app.route('/api/top-groups')
def api_top_groups():
    """API: топ групп по активности."""
    conn = get_db_connection()

    rows = conn.execute("""
        SELECT
            g.title,
            g.username,
            g.members_count,
            g.status,
            COUNT(i.id) as message_count
        FROM target_groups g
        LEFT JOIN interactions i ON g.id = i.group_id
        GROUP BY g.id
        ORDER BY message_count DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    return jsonify([{
        "title": row["title"],
        "username": row["username"],
        "members": row["members_count"],
        "status": row["status"],
        "messages": row["message_count"]
    } for row in rows])


# ============================================
# ГРУППЫ
# ============================================

@app.route('/groups')
def groups_page():
    """Страница со списком групп."""
    return render_template('groups.html')


@app.route('/api/groups/categories')
def api_groups_categories():
    """Список категорий с количеством групп (для фильтра в UI)."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT COALESCE(source_category, '') as category, COUNT(*) as cnt
        FROM target_groups
        GROUP BY COALESCE(source_category, '')
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()
    return jsonify([{"category": r["category"] or None, "count": r["cnt"]} for r in rows])


@app.route('/api/groups')
def api_groups():
    """API: список всех групп с фильтрами."""
    status_filter = request.args.get('status', 'all')
    category_filter = (request.args.get('category') or '').strip()
    conn = get_db_connection()

    base_sql = """
        SELECT
            g.*,
            SUM(CASE WHEN COALESCE(i.interaction_type,'') != 'view' THEN 1 ELSE 0 END) as message_count,
            COALESCE(SUM(CASE
                WHEN i.interaction_type='view' AND i.status='ok' AND i.response_text LIKE '+%'
                THEN CAST(SUBSTR(i.response_text, 2, INSTR(i.response_text,' ')-2) AS INTEGER)
                ELSE 0 END), 0) as views_count,
            SUM(CASE WHEN i.interaction_type='reaction' AND i.status='ok' THEN 1 ELSE 0 END) as reactions_count,
            (SELECT COUNT(DISTINCT a.id) FROM agent_accounts a
              JOIN agent_group_membership m ON m.agent_id = a.id
              WHERE a.views_enabled = 1
                AND a.status NOT IN ('banned','disabled')
                AND m.group_id = g.id
                AND m.status IN ('joined','active')) as views_active_agents
        FROM target_groups g
        LEFT JOIN interactions i ON g.id = i.group_id
    """
    where = []
    params = []
    if status_filter != 'all':
        where.append("g.status = ?"); params.append(status_filter)
    if category_filter:
        if category_filter == '__none__':
            where.append("g.source_category IS NULL")
        else:
            where.append("g.source_category = ?"); params.append(category_filter)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(base_sql + where_sql + " GROUP BY g.id ORDER BY g.added_at DESC", tuple(params)).fetchall()

    conn.close()

    result = []
    for row in rows:
        d = dict(row)
        result.append({
            "id": d.get("id"),
            "telegram_group_id": d.get("telegram_group_id"),
            "title": d.get("title"),
            "username": d.get("username"),
            "description": d.get("description"),
            "members_count": d.get("members_count"),
            "status": d.get("status"),
            "added_at": d.get("added_at"),
            "last_monitored": d.get("last_monitored"),
            "message_count": d.get("message_count"),
            "views_count": int(d.get("views_count") or 0),
            "views_active_agents": int(d.get("views_active_agents") or 0),
            "reactions_count": int(d.get("reactions_count") or 0),
            "is_channel": bool(d.get("is_channel") or 0),
            "linked_chat_id": d.get("linked_chat_id"),
            "assigned_agent_id": d.get("assigned_agent_id"),
            "source_keyword": d.get("source_keyword"),
            "source_category": d.get("source_category"),
        })
    return jsonify(result)


# ============================================
# СООБЩЕНИЯ / ВЗАИМОДЕЙСТВИЯ
# ============================================

@app.route('/messages')
def messages_page():
    """Страница со списком сообщений."""
    return render_template('messages.html')


@app.route('/proactive')
def proactive_page():
    """Страница с проактивными постами."""
    return render_template('proactive.html')


@app.route('/users')
def users_page():
    """Страница с профилями активных юзеров."""
    return render_template('users.html')


@app.route('/api/users')
def api_users():
    """API: список профилей юзеров."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)

    status = request.args.get("status")
    min_messages = int(request.args.get("min_messages", 0))
    limit = int(request.args.get("limit", 200))

    users = db.get_user_profiles(limit=limit, status=status if status else None,
                                 min_messages=min_messages)
    return jsonify(users)


@app.route('/api/users/<int:user_id>')
def api_user_detail(user_id):
    """API: детальная инфа о юзере + история его сообщений."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)

    profiles = db.get_user_profiles(limit=1)
    profile = next((p for p in db.get_user_profiles(limit=10000) if p["id"] == user_id), None)
    if not profile:
        return jsonify({"error": "Not found"}), 404

    messages = db.get_user_messages(user_id, limit=200)
    return jsonify({
        "profile": profile,
        "messages": messages,
    })


@app.route('/api/users/<int:user_id>/status', methods=['POST'])
def api_set_user_status(user_id):
    """API: меняет статус юзера (new / engaged / converted / blocked)."""
    data = request.get_json()
    new_status = (data.get("status") or "").strip()
    notes = data.get("notes")

    if new_status not in ("new", "engaged", "converted", "blocked"):
        return jsonify({"success": False, "error": "Invalid status"}), 400

    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    db.update_user_status(user_id, new_status, notes)
    return jsonify({"success": True})


@app.route('/api/users/stats')
def api_users_stats():
    """API: общая статистика по юзерам."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    return jsonify(db.get_users_stats())


@app.route('/api/messages')
def api_messages():
    """API: история сообщений."""
    group_id = request.args.get('group_id', type=int)
    limit = request.args.get('limit', 100, type=int)

    conn = get_db_connection()

    if group_id:
        rows = conn.execute("""
            SELECT
                i.*,
                g.title as group_title,
                g.username as group_username
            FROM interactions i
            LEFT JOIN target_groups g ON i.group_id = g.id
            WHERE i.group_id = ?
            ORDER BY i.created_at DESC
            LIMIT ?
        """, (group_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                i.*,
                g.title as group_title,
                g.username as group_username
            FROM interactions i
            LEFT JOIN target_groups g ON i.group_id = g.id
            ORDER BY i.created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

    conn.close()

    return jsonify([{
        "id": row["id"],
        "agent_id": row["agent_id"],
        "group_id": row["group_id"],
        "group_title": row["group_title"],
        "group_username": row["group_username"],
        "user_id": row["user_id"],
        "message_text": row["message_text"],
        "response_text": row["response_text"],
        "interaction_type": row["interaction_type"],
        "status": row["status"],
        "created_at": row["created_at"]
    } for row in rows])


# ============================================
# АГЕНТЫ
# ============================================

@app.route('/api/proactive-posts')
def api_proactive_posts():
    """API: история проактивных постов."""
    limit = request.args.get('limit', 50, type=int)

    conn = get_db_connection()

    rows = conn.execute("""
        SELECT
            p.*,
            g.title as group_title,
            g.username as group_username
        FROM proactive_posts p
        LEFT JOIN target_groups g ON p.group_id = g.id
        ORDER BY p.created_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    return jsonify([{
        "id": row["id"],
        "agent_id": row["agent_id"],
        "group_id": row["group_id"],
        "group_title": row["group_title"],
        "group_username": row["group_username"],
        "post_text": row["post_text"],
        "template_used": row["template_used"],
        "created_at": row["created_at"]
    } for row in rows])


@app.route('/agents')
def agents_page():
    """Страница агентов."""
    return render_template('agents.html')


@app.route('/api/agents')
def api_agents():
    """API: информация об аккаунтах агентов."""
    conn = get_db_connection()

    rows = conn.execute("""
        SELECT
            a.*,
            (SELECT COUNT(*) FROM interactions WHERE agent_id = a.id) as total_messages,
            ((SELECT COUNT(*) FROM interactions WHERE agent_id = a.id AND date(created_at) = date('now')) +
             (SELECT COUNT(*) FROM proactive_posts WHERE agent_id = a.id AND date(created_at) = date('now'))) as today_messages,
            (SELECT COUNT(*) FROM proactive_posts WHERE agent_id = a.id) as proactive_total,
            (SELECT COUNT(*) FROM proactive_posts WHERE agent_id = a.id AND date(created_at) = date('now')) as proactive_today
        FROM agent_accounts a
    """).fetchall()

    # Добавим memberships для каждого агента
    membership_rows = conn.execute("""
        SELECT agent_id, status, COUNT(*) as cnt
        FROM agent_group_membership
        GROUP BY agent_id, status
    """).fetchall()

    membership_by_agent = {}
    for r in membership_rows:
        if r["agent_id"] not in membership_by_agent:
            membership_by_agent[r["agent_id"]] = {}
        membership_by_agent[r["agent_id"]][r["status"]] = r["cnt"]

    # Кол-во назначенных групп для каждого агента.
    assigned_rows = conn.execute("""
        SELECT assigned_agent_id, COUNT(*) as cnt
        FROM target_groups
        WHERE assigned_agent_id IS NOT NULL
        GROUP BY assigned_agent_id
    """).fetchall()
    assigned_by_agent = {r["assigned_agent_id"]: r["cnt"] for r in assigned_rows}

    conn.close()

    result = []
    for row in rows:
        d = dict(row)
        result.append({
            "id": d.get("id"),
            "phone_number": d.get("phone_number"),
            "session_name": d.get("session_name"),
            "status": d.get("status"),
            "proxy_ip": d.get("proxy_ip"),
            "referral_target": d.get("referral_target"),
            "created_at": d.get("created_at"),
            "last_active": d.get("last_active"),
            "total_messages": d.get("total_messages"),
            "today_messages": d.get("today_messages"),
            "proactive_total": d.get("proactive_total"),
            "proactive_today": d.get("proactive_today"),
            "memberships": membership_by_agent.get(d.get("id"), {}),
            "assigned_groups_count": assigned_by_agent.get(d.get("id"), 0),
            "reactions_enabled": bool(d.get("reactions_enabled") if d.get("reactions_enabled") is not None else 1),
            "views_enabled": bool(d.get("views_enabled")) if d.get("views_enabled") is not None else False,
        })
    return jsonify(result)


# ============================================
# НАЗНАЧЕНИЕ ГРУПП АГЕНТАМ / РЕАКЦИИ
# ============================================

@app.route('/api/agents/<int:agent_id>/groups')
def api_agent_groups(agent_id):
    """Список групп, назначенных агенту (для блока в карточке)."""
    from agent_database import AgentDatabase
    from config_agent import DB_PATH as _DB
    db = AgentDatabase(_DB)
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT g.id, g.title, g.username, g.members_count, g.status,
               g.assigned_agent_id,
               (SELECT COUNT(*) FROM proactive_posts
                  WHERE group_id = g.id AND agent_id = ?
                  AND created_at > datetime('now', '-24 hours')) as posts_24h
        FROM target_groups g
        WHERE g.assigned_agent_id = ?
        ORDER BY g.title
    """, (agent_id, agent_id)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/groups/<int:group_id>/assign', methods=['POST'])
def api_assign_group(group_id):
    """Назначить группу агенту. {agent_id: int|null}."""
    from agent_database import AgentDatabase
    from config_agent import DB_PATH as _DB
    data = request.get_json(force=True) or {}
    aid = data.get('agent_id')
    if aid in ('', 'null'):
        aid = None
    if aid is not None:
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad agent_id"}), 400
    db = AgentDatabase(_DB)
    ok = db.assign_group(group_id, aid)
    return jsonify({"ok": ok, "group_id": group_id, "agent_id": aid})


@app.route('/api/groups/assign-by-category', methods=['POST'])
def api_assign_by_category():
    """Назначает агенту все группы из указанной категории.
    body: {agent_id: int, category: str, only_unassigned?: bool=true}
    category может быть '__none__' — назначить группы без категории."""
    data = request.get_json(force=True) or {}
    try:
        aid = int(data.get("agent_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "agent_id required"}), 400
    category = (data.get("category") or "").strip()
    if not category:
        return jsonify({"ok": False, "error": "category required"}), 400
    only_unassigned = bool(data.get("only_unassigned", True))
    strict = bool(data.get("strict", False))  # B: снять группы других категорий у этого агента

    conn = get_db_connection()
    unassigned_count = 0
    if strict:
        # Сначала открепляем от агента всё, что НЕ из выбранной категории
        if category == "__none__":
            cur = conn.execute(
                "UPDATE target_groups SET assigned_agent_id=NULL "
                "WHERE assigned_agent_id=? AND source_category IS NOT NULL",
                (aid,))
        else:
            cur = conn.execute(
                "UPDATE target_groups SET assigned_agent_id=NULL "
                "WHERE assigned_agent_id=? AND (source_category != ? OR source_category IS NULL)",
                (aid, category))
        unassigned_count = cur.rowcount
        # При strict — назначаем все группы категории на агента
        # (включая ранее закреплённые за другими — это явное действие)
        only_unassigned = False

    where = []
    params = []
    if category == "__none__":
        where.append("source_category IS NULL")
    else:
        where.append("source_category = ?"); params.append(category)
    if only_unassigned:
        where.append("assigned_agent_id IS NULL")
    where_sql = " AND ".join(where)
    cursor = conn.execute(f"UPDATE target_groups SET assigned_agent_id = ? WHERE {where_sql}",
                          (aid, *params))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "agent_id": aid, "category": category,
                    "assigned": affected, "unassigned": unassigned_count})


@app.route('/api/groups/auto-distribute', methods=['POST'])
def api_auto_distribute():
    """Round-robin распределение по активным агентам.
    Body: {only_unassigned: bool=false, agent_ids?: [int]}"""
    from agent_database import AgentDatabase
    from config_agent import DB_PATH as _DB
    data = request.get_json(silent=True) or {}
    only_unassigned = bool(data.get('only_unassigned', False))
    agent_ids = data.get('agent_ids')
    db = AgentDatabase(_DB)
    if not agent_ids:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT id FROM agent_accounts "
            "WHERE phone_number != 'placeholder' AND status != 'banned' "
            "ORDER BY id"
        ).fetchall()
        agent_ids = [r["id"] for r in rows]
        conn.close()
    if not agent_ids:
        return jsonify({"ok": False, "error": "no agents"}), 400
    n = db.auto_distribute_groups(agent_ids, only_unassigned=only_unassigned)
    return jsonify({"ok": True, "assigned": n, "agents": agent_ids})


@app.route('/api/scout-runs')
def api_scout_runs():
    """История прогонов парсинга групп."""
    from agent_database import AgentDatabase
    from config_agent import DB_PATH as _DB
    limit = int(request.args.get('limit', 30))
    db = AgentDatabase(_DB)
    runs = db.get_scout_runs(limit=limit)
    return jsonify(runs)


@app.route('/api/agents/<int:agent_id>/reactions', methods=['POST'])
def api_set_reactions(agent_id):
    """Включить/выключить реакции для агента. {enabled: bool}."""
    from agent_database import AgentDatabase
    from config_agent import DB_PATH as _DB
    data = request.get_json(force=True) or {}
    enabled = bool(data.get('enabled'))
    db = AgentDatabase(_DB)
    ok = db.set_agent_reactions_enabled(agent_id, enabled)
    return jsonify({"ok": ok, "agent_id": agent_id, "reactions_enabled": enabled})


@app.route('/api/agents/import-tdata', methods=['POST'])
def api_import_tdata():
    """Импорт TG-аккаунта из tdata.

    Принимает либо multipart-загрузку zip (`tdata_zip`), либо серверный путь
    (`tdata_path` form-field). Остальные поля: passcode, proxy, name,
    referral_target.
    """
    import os, shutil, tempfile, zipfile, sqlite3

    passcode = (request.form.get("passcode") or "").strip() or None
    proxy_url = (request.form.get("proxy") or "").strip() or None
    name_override = (request.form.get("name") or "").strip() or None
    referral_target = (request.form.get("referral_target") or "").strip() or None

    tmp_root: str | None = None
    try:
        from agent_database import AgentDatabase
        db = AgentDatabase(DB_PATH)
        conn = sqlite3.connect(db.db_path)
        count = conn.execute("SELECT COUNT(*) FROM agent_accounts").fetchone()[0]
        conn.close()

        session_name = name_override or f"agent_{count + 1}"

        # Определяем где взять tdata: загрузка или path
        tdata_dir: str
        uploaded = request.files.get("tdata_zip")
        path_field = (request.form.get("tdata_path") or "").strip()

        if uploaded and uploaded.filename:
            tmp_root = tempfile.mkdtemp(prefix="tdata_upload_")
            extract_dir = os.path.join(tmp_root, "extracted")
            os.makedirs(extract_dir)
            zip_path = os.path.join(tmp_root, "in.zip")
            uploaded.save(zip_path)
            # Безопасная распаковка (без path traversal)
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    if member.startswith("/") or ".." in member.split("/"):
                        return jsonify({"success": False, "error": f"Небезопасный путь в архиве: {member}"}), 400
                zf.extractall(extract_dir)
            # Найти папку с key_datas внутри extracted
            tdata_dir = _find_tdata_root(extract_dir)
            if not tdata_dir:
                return jsonify({"success": False, "error": "В архиве не найдена папка tdata (нет файла key_datas)"}), 400
        elif path_field:
            if not os.path.isdir(path_field):
                return jsonify({"success": False, "error": f"Папка не найдена: {path_field}"}), 400
            tdata_dir = _find_tdata_root(path_field) or path_field
        else:
            return jsonify({"success": False, "error": "Загрузите tdata.zip или укажите tdata_path"}), 400

        # Конвертация через opentele + сохранение в agent_accounts
        result = asyncio.run(_run_tdata_import(
            tdata_dir=tdata_dir, passcode=passcode, proxy_url=proxy_url,
            session_name=session_name, db_path=db.db_path,
            referral_target=referral_target,
        ))
        return jsonify(result), (200 if result.get("success") else 400)

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if tmp_root and os.path.isdir(tmp_root):
            try:
                import shutil as _sh
                _sh.rmtree(tmp_root, ignore_errors=True)
            except Exception:
                pass


def _find_tdata_root(start_dir: str) -> str | None:
    """Ищет реальную папку tdata (содержащую key_datas) под start_dir."""
    import os
    for root, dirs, files in os.walk(start_dir):
        if "key_datas" in files:
            return root
    return None


async def _run_tdata_import(tdata_dir: str, passcode, proxy_url, session_name,
                            db_path: str, referral_target):
    """Конвертирует tdata → Pyrogram session и регистрирует в БД."""
    import os, sqlite3

    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
    except ImportError:
        return {"success": False, "error": "opentele не установлен. pip install opentele"}

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_path = os.path.join(SESSIONS_DIR, session_name)

    try:
        tdesk = TDesktop(tdata_dir, passcode=passcode) if passcode else TDesktop(tdata_dir)
    except Exception as e:
        return {"success": False, "error": f"Не удалось открыть tdata: {e}"}

    if not tdesk.isLoaded():
        return {"success": False, "error": "tdata не загрузился (неверный passcode?)"}

    try:
        client = await tdesk.ToPyrogram(session=session_path, flag=UseCurrentSession, api=None)
    except Exception as e:
        return {"success": False, "error": f"Конвертация в Pyrogram session не удалась: {e}"}

    # Применяем прокси, если задан
    from multi_agent import _parse_proxy_url
    pd = _parse_proxy_url(proxy_url)
    if pd:
        client.proxy = pd

    try:
        await client.start()
        me = await client.get_me()
        phone = (me.phone_number and f"+{me.phone_number}") or f"tdata_{me.id}"
        device_model = getattr(client, "device_model", None)
        system_version = getattr(client, "system_version", None)
        app_version = getattr(client, "app_version", None)
        await client.stop()
    except Exception as e:
        try:
            await client.stop()
        except Exception:
            pass
        return {"success": False, "error": f"Старт session не удался: {e}"}

    # Сохраняем в БД
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO agent_accounts (phone_number, session_name, status, proxy_url, '
            'device_model, system_version, app_version, referral_target) '
            'VALUES (?, ?, "active", ?, ?, ?, ?, ?)',
            (phone, session_name, proxy_url, device_model, system_version, app_version, referral_target),
        )
        conn.commit()
        agent_id = cur.lastrowid
    except sqlite3.IntegrityError as e:
        conn.close()
        return {"success": False, "error": f"Этот номер/session уже в БД: {e}"}
    finally:
        conn.close()

    return {
        "success": True,
        "agent_id": agent_id,
        "phone": phone,
        "session_name": session_name,
        "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
        "username": me.username,
        "message": f"✅ Импортирован {phone} (id={me.id}). Перезапусти агентов: ./start.sh",
    }


# ============================================================
# SMS-логин из дашборда (двухшаговый: send_code → sign_in)
# ============================================================
import threading as _threading
import uuid as _uuid

_SMS_LOOP = None
_SMS_LOOP_LOCK = _threading.Lock()
_SMS_SESSIONS: dict = {}  # login_id -> {client, phone, phone_code_hash, session_name, proxy_url, referral_target}


def _get_sms_loop():
    global _SMS_LOOP
    with _SMS_LOOP_LOCK:
        if _SMS_LOOP and _SMS_LOOP.is_running():
            return _SMS_LOOP
        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = _threading.Thread(target=_run, name="sms-login-loop", daemon=True)
        t.start()
        _SMS_LOOP = loop
        return loop


def _sms_submit(coro):
    """Запускает coroutine в фоновом event loop и возвращает результат."""
    loop = _get_sms_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=120)


async def _sms_start_async(phone: str, proxy_url, session_name: str):
    from pyrogram import Client
    from config_agent import TELEGRAM_API_ID, TELEGRAM_API_HASH
    from multi_agent import _parse_proxy_url
    import os
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    kwargs = dict(
        name=session_name, api_id=TELEGRAM_API_ID, api_hash=TELEGRAM_API_HASH,
        phone_number=phone, workdir=SESSIONS_DIR,
    )
    pd = _parse_proxy_url(proxy_url)
    if pd:
        kwargs["proxy"] = pd

    client = Client(**kwargs)
    await client.connect()
    sent = await client.send_code(phone)
    return client, sent.phone_code_hash


async def _sms_confirm_async(client, phone: str, phone_code_hash: str, code: str, password):
    from pyrogram.errors import SessionPasswordNeeded
    try:
        await client.sign_in(phone, phone_code_hash, code)
    except SessionPasswordNeeded:
        if not password:
            return {"need_password": True}
        await client.check_password(password)
    me = await client.get_me()
    # Завершаем connect(): disconnect сохранит session-file
    await client.disconnect()
    return {
        "id": me.id,
        "phone": (me.phone_number and f"+{me.phone_number}") or phone,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "username": me.username,
    }


async def _sms_cancel_async(client):
    try:
        await client.disconnect()
    except Exception:
        pass


@app.route('/api/agents/sms/start', methods=['POST'])
def api_sms_start():
    """Шаг 1: отправить SMS-код на номер. Возвращает login_id для шага 2."""
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    proxy_url = (data.get("proxy") or "").strip() or None
    name_override = (data.get("name") or "").strip() or None
    referral_target = (data.get("referral_target") or "").strip() or None

    if not phone.startswith("+"):
        return jsonify({"success": False, "error": "Номер должен начинаться с +"}), 400

    try:
        from agent_database import AgentDatabase
        db = AgentDatabase(DB_PATH)
        conn = sqlite3.connect(db.db_path)
        count = conn.execute("SELECT COUNT(*) FROM agent_accounts").fetchone()[0]
        conn.close()
        session_name = name_override or f"agent_{count + 1}"

        client, phone_code_hash = _sms_submit(
            _sms_start_async(phone, proxy_url, session_name)
        )
    except Exception as e:
        return jsonify({"success": False, "error": f"Не удалось отправить код: {e}"}), 400

    login_id = _uuid.uuid4().hex
    _SMS_SESSIONS[login_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "session_name": session_name,
        "proxy_url": proxy_url,
        "referral_target": referral_target,
    }
    return jsonify({
        "success": True,
        "login_id": login_id,
        "session_name": session_name,
        "message": f"Код отправлен на {phone}",
    })


@app.route('/api/agents/sms/confirm', methods=['POST'])
def api_sms_confirm():
    """Шаг 2: подтвердить код (и 2FA-пароль, если включён)."""
    data = request.get_json(silent=True) or {}
    login_id = (data.get("login_id") or "").strip()
    code = (data.get("code") or "").strip()
    password = data.get("password") or None

    sess = _SMS_SESSIONS.get(login_id)
    if not sess:
        return jsonify({"success": False, "error": "login_id не найден или истёк. Начните заново."}), 400
    if not code:
        return jsonify({"success": False, "error": "Введите код"}), 400

    try:
        result = _sms_submit(_sms_confirm_async(
            sess["client"], sess["phone"], sess["phone_code_hash"], code, password,
        ))
    except Exception as e:
        return jsonify({"success": False, "error": f"Ошибка входа: {e}"}), 400

    if result.get("need_password"):
        return jsonify({"success": False, "need_password": True,
                        "error": "Включена 2FA — введите облачный пароль."}), 200

    # Сохраняем в БД
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO agent_accounts (phone_number, session_name, status, proxy_url, referral_target) '
            'VALUES (?, ?, "active", ?, ?)',
            (result["phone"], sess["session_name"], sess["proxy_url"], sess["referral_target"]),
        )
        conn.commit()
        agent_id = cur.lastrowid
        conn.close()
    except sqlite3.IntegrityError as e:
        _SMS_SESSIONS.pop(login_id, None)
        return jsonify({"success": False, "error": f"Этот номер/session уже в БД: {e}"}), 400

    _SMS_SESSIONS.pop(login_id, None)
    name = f"{result.get('first_name') or ''} {result.get('last_name') or ''}".strip()
    return jsonify({
        "success": True,
        "agent_id": agent_id,
        "phone": result["phone"],
        "session_name": sess["session_name"],
        "name": name,
        "username": result.get("username"),
        "message": f"✅ Добавлен {result['phone']} (id={result['id']}). Перезапусти агентов: ./start.sh",
    })


@app.route('/api/agents/sms/cancel', methods=['POST'])
def api_sms_cancel():
    data = request.get_json(silent=True) or {}
    login_id = (data.get("login_id") or "").strip()
    sess = _SMS_SESSIONS.pop(login_id, None)
    if sess:
        try:
            _sms_submit(_sms_cancel_async(sess["client"]))
        except Exception:
            pass
    return jsonify({"success": True})


@app.route('/api/agents/referral', methods=['POST'])
def api_set_agent_referral():
    """API: меняет что промоутит агент."""
    data = request.get_json()
    agent_id = int(data.get("agent_id", 0))
    target = (data.get("target") or "").strip()

    if not agent_id or not target:
        return jsonify({"success": False, "error": "Укажите agent_id и target"}), 400

    try:
        from agent_database import AgentDatabase
        db = AgentDatabase(DB_PATH)
        db.update_agent_referral(agent_id, target)
        return jsonify({
            "success": True,
            "message": f"✅ Agent #{agent_id} теперь промоутит {target}",
            "info": "Перезапусти агентов чтобы изменения применились."
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/groups/add', methods=['POST'])
def api_add_group():
    """API: добавление группы в очередь. Агент обработает её при следующем join cycle."""
    data = request.get_json()
    username = (data.get("username") or "").strip().lstrip("@")

    if not username:
        return jsonify({"success": False, "error": "Укажите username"}), 400

    try:
        # Импортируем БД здесь чтобы не было проблем с многопоточностью
        from agent_database import AgentDatabase
        db = AgentDatabase(DB_PATH)

        pid = db.add_pending_group(username)

        return jsonify({
            "success": True,
            "queued_id": pid,
            "message": f"📋 Группа @{username} добавлена в очередь. Агент обработает её в ближайшие 30 секунд.",
            "info": "После обработки группа появится в списке. Проверь через 1 минуту."
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/groups/search', methods=['POST'])
def api_search_groups():
    """API: добавить запрос на парсинг групп по ключевым словам в очередь."""
    data = request.get_json()
    keywords_input = (data.get("keywords") or "").strip()
    max_results = int(data.get("max_results") or 10)
    source_category = (data.get("category") or "").strip() or None

    if not keywords_input:
        return jsonify({"success": False, "error": "Укажите ключевые слова"}), 400

    # Чистим: разделяем по запятой или новой строке, убираем пустые
    keywords = [k.strip() for k in keywords_input.replace("\n", ",").split(",") if k.strip()]

    if not keywords:
        return jsonify({"success": False, "error": "Не нашли ни одного валидного слова"}), 400

    if len(keywords) > 20:
        return jsonify({"success": False, "error": "Максимум 20 ключевых слов за раз"}), 400

    if max_results < 1 or max_results > 50:
        max_results = 10

    try:
        from agent_database import AgentDatabase
        db = AgentDatabase(DB_PATH)
        keywords_str = ",".join(keywords)
        sid = db.add_pending_search(keywords_str, max_results, source_category=source_category)

        return jsonify({
            "success": True,
            "queued_id": sid,
            "keywords_count": len(keywords),
            "keywords": keywords,
            "message": f"📋 Поиск по {len(keywords)} ключевым словам добавлен в очередь.",
            "info": "Агент обработает запрос в ближайшие 30 секунд."
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/keywords')
def api_get_keywords():
    """API: получить все слова сгруппированные по категориям."""
    from agent_database import AgentDatabase
    from config_agent import SCOUT_KEYWORD_POOL

    db = AgentDatabase(DB_PATH)
    db.seed_keyword_pool(SCOUT_KEYWORD_POOL)

    all_kw = db.get_all_keywords(only_enabled=False)
    grouped = {}
    for kw in all_kw:
        cat = kw["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(kw)
    return jsonify(grouped)


@app.route('/api/keywords/category/<category>')
def api_get_keywords_by_category(category):
    """API: слова конкретной категории."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    all_kw = db.get_all_keywords(only_enabled=False)
    items = [kw for kw in all_kw if kw["category"] == category]
    return jsonify({"category": category, "items": items})


@app.route('/api/keywords/add', methods=['POST'])
def api_add_keyword():
    """API: добавить новое слово."""
    data = request.get_json()
    category = (data.get("category") or "").strip()
    keyword = (data.get("keyword") or "").strip()

    if not category or not keyword:
        return jsonify({"success": False, "error": "Укажи category и keyword"}), 400

    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    kid = db.add_keyword(category, keyword)
    if kid > 0:
        return jsonify({"success": True, "id": kid})
    else:
        return jsonify({"success": False, "error": "Слово уже существует в этой категории"}), 400


@app.route('/api/keywords/<int:kid>', methods=['PUT'])
def api_update_keyword(kid):
    """API: редактирование слова или включение/выключение."""
    data = request.get_json()
    keyword = data.get("keyword")
    enabled = data.get("enabled")
    category = data.get("category")

    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    ok = db.update_keyword(kid, keyword=keyword, enabled=enabled, category=category)
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Не удалось обновить"}), 400


@app.route('/api/keywords/<int:kid>', methods=['DELETE'])
def api_delete_keyword(kid):
    """API: удалить слово."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    ok = db.delete_keyword(kid)
    return jsonify({"success": ok})


@app.route('/api/keywords/category/<category>/rename', methods=['POST'])
def api_rename_category(category):
    """API: переименовать категорию."""
    data = request.get_json()
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"success": False, "error": "Укажи new_name"}), 400

    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    affected = db.rename_category(category, new_name)
    return jsonify({"success": True, "affected": affected})


@app.route('/api/keywords/category/<category>', methods=['DELETE'])
def api_delete_category(category):
    """API: удалить всю категорию."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    affected = db.delete_category(category)
    return jsonify({"success": True, "deleted": affected})


@app.route('/api/bans')
def api_bans():
    """API: список банов и причин для аналитики."""
    conn = get_db_connection()

    # Группы со статусом banned + последняя ошибка из membership
    rows = conn.execute("""
        SELECT
            g.id, g.title, g.username, g.members_count, g.last_monitored, g.status,
            g.is_channel,
            (SELECT m.error_message FROM agent_group_membership m
             WHERE m.group_id = g.id AND m.status = 'banned'
             ORDER BY m.last_attempt DESC LIMIT 1) as ban_reason,
            (SELECT m.last_attempt FROM agent_group_membership m
             WHERE m.group_id = g.id AND m.status = 'banned'
             ORDER BY m.last_attempt DESC LIMIT 1) as banned_at,
            (SELECT m.agent_id FROM agent_group_membership m
             WHERE m.group_id = g.id AND m.status = 'banned'
             ORDER BY m.last_attempt DESC LIMIT 1) as banned_agent
        FROM target_groups g
        WHERE g.status = 'banned'
        ORDER BY banned_at DESC
        LIMIT 100
    """).fetchall()

    bans = []
    for row in rows:
        d = dict(row)
        bans.append({
            "id": d.get("id"),
            "title": d.get("title"),
            "username": d.get("username"),
            "members_count": d.get("members_count"),
            "is_channel": bool(d.get("is_channel") or 0),
            "ban_reason": d.get("ban_reason"),
            "banned_at": d.get("banned_at"),
            "banned_agent": d.get("banned_agent"),
        })

    # Также добавим failed (не получилось писать)
    failed_rows = conn.execute("""
        SELECT g.id, g.title, g.username, g.status,
               COUNT(m.id) as failed_attempts
        FROM target_groups g
        LEFT JOIN agent_group_membership m ON m.group_id = g.id AND m.status IN ('failed','banned')
        WHERE g.status IN ('no_permission', 'private', 'invalid', 'banned')
        GROUP BY g.id
        ORDER BY failed_attempts DESC
    """).fetchall()

    by_status = {}
    for row in failed_rows:
        d = dict(row)
        s = d["status"]
        by_status[s] = by_status.get(s, 0) + 1

    conn.close()

    # Группируем причины
    reasons = {}
    for b in bans:
        r = b.get("ban_reason") or "unknown"
        # Извлекаем тип ошибки из строки Pyrogram
        for key in ["USER_BANNED_IN_CHANNEL", "USER_KICKED", "CHAT_WRITE_FORBIDDEN",
                    "CHANNEL_PRIVATE", "USERNAME_INVALID", "PEER_FLOOD"]:
            if key in r:
                reasons[key] = reasons.get(key, 0) + 1
                break
        else:
            reasons["other"] = reasons.get("other", 0) + 1

    return jsonify({
        "bans": bans,
        "by_status": by_status,
        "reasons": reasons,
        "total_problematic": len(bans) + by_status.get("no_permission", 0) +
                             by_status.get("private", 0) + by_status.get("invalid", 0),
    })


@app.route('/api/lessons')
def api_lessons():
    """API: список выученных правил."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    lessons = db.get_ban_lessons(only_enabled=False)
    return jsonify(lessons)


@app.route('/api/lessons/<int:lid>', methods=['PUT'])
def api_update_lesson(lid):
    """API: обновить правило."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    data = request.get_json()
    ok = db.update_ban_lesson(lid, **data)
    return jsonify({"success": ok})


@app.route('/api/lessons/<int:lid>', methods=['DELETE'])
def api_delete_lesson(lid):
    """API: удалить правило."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    return jsonify({"success": db.delete_ban_lesson(lid)})


@app.route('/api/lessons/add', methods=['POST'])
def api_add_lesson():
    """API: добавить новое правило вручную."""
    data = request.get_json()
    phrase = (data.get("forbidden_phrase") or "").strip()
    if not phrase:
        return jsonify({"success": False, "error": "Укажи forbidden_phrase"}), 400

    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    lid = db.add_ban_lesson(
        forbidden_phrase=phrase,
        error_type=data.get("error_type", "USER_BANNED_IN_CHANNEL"),
        topic=data.get("topic"),
        recommendation=data.get("recommendation", "Добавлено вручную"),
        auto_learned=False,
    )
    return jsonify({"success": lid > 0, "id": lid})


@app.route('/api/bans/<int:group_id>')
def api_ban_detail(group_id):
    """API: детальная инфа по одному забаненному ресурсу."""
    conn = get_db_connection()

    # Сама группа
    grow = conn.execute("""
        SELECT id, title, username, members_count, status, is_channel,
               telegram_group_id, description, added_at, last_monitored
        FROM target_groups WHERE id = ?
    """, (group_id,)).fetchone()

    if not grow:
        conn.close()
        return jsonify({"error": "Group not found"}), 404

    group_data = dict(grow)

    # Все попытки в этой группе (membership)
    attempts = conn.execute("""
        SELECT m.agent_id, m.status, m.error_message, m.last_attempt,
               a.phone_number, a.referral_target
        FROM agent_group_membership m
        LEFT JOIN agent_accounts a ON a.id = m.agent_id
        WHERE m.group_id = ?
        ORDER BY m.last_attempt DESC
    """, (group_id,)).fetchall()

    # Последние сообщения которые отправляли в эту группу (если есть)
    interactions = conn.execute("""
        SELECT id, agent_id, message_text, response_text, status, created_at
        FROM interactions
        WHERE group_id = ?
        ORDER BY created_at DESC LIMIT 10
    """, (group_id,)).fetchall()

    conn.close()

    return jsonify({
        "group": {
            "id": group_data.get("id"),
            "title": group_data.get("title"),
            "username": group_data.get("username"),
            "members_count": group_data.get("members_count"),
            "status": group_data.get("status"),
            "is_channel": bool(group_data.get("is_channel") or 0),
            "telegram_group_id": group_data.get("telegram_group_id"),
            "description": group_data.get("description"),
            "added_at": group_data.get("added_at"),
            "last_monitored": group_data.get("last_monitored"),
        },
        "attempts": [{
            "agent_id": dict(r).get("agent_id"),
            "agent_phone": dict(r).get("phone_number"),
            "referral_target": dict(r).get("referral_target"),
            "status": dict(r).get("status"),
            "error_message": dict(r).get("error_message"),
            "last_attempt": dict(r).get("last_attempt"),
        } for r in attempts],
        "interactions": [{
            "id": dict(r).get("id"),
            "agent_id": dict(r).get("agent_id"),
            "message_text": dict(r).get("message_text"),
            "response_text": dict(r).get("response_text"),
            "status": dict(r).get("status"),
            "created_at": dict(r).get("created_at"),
        } for r in interactions],
    })


@app.route('/api/scout-schedule')
def api_scout_schedule():
    """API: расписание автоматического парсинга."""
    from datetime import datetime, timedelta
    from config_agent import (
        AUTO_SCOUT_ENABLED, AUTO_SCOUT_TIMES, AUTO_SCOUT_KEYWORDS_PER_RUN,
        SCOUT_KEYWORD_POOL
    )
    from agent_database import AgentDatabase

    if not AUTO_SCOUT_ENABLED:
        return jsonify({"enabled": False})

    # Читаем актуальный пул из БД (с возможными правками юзера)
    db = AgentDatabase(DB_PATH)
    db.seed_keyword_pool(SCOUT_KEYWORD_POOL)  # сидим если пусто
    keywords = db.get_all_keywords(only_enabled=True)

    # Группируем по категориям
    categories_dict = {}
    for kw in keywords:
        categories_dict[kw["category"]] = categories_dict.get(kw["category"], 0) + 1

    total_keywords = len(keywords)

    # Вычисляем следующие запуски
    now = datetime.now()
    next_runs = []
    for hour in sorted(AUTO_SCOUT_TIMES):
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        next_runs.append(next_run.isoformat())

    next_runs.sort()

    from config_agent import TIMEZONE_LABEL
    return jsonify({
        "enabled": True,
        "times_per_day": len(AUTO_SCOUT_TIMES),
        "scheduled_hours": AUTO_SCOUT_TIMES,
        "keywords_per_run": AUTO_SCOUT_KEYWORDS_PER_RUN,
        "total_keywords_in_pool": total_keywords,
        "categories": categories_dict,
        "next_runs": next_runs,
        "timezone": TIMEZONE_LABEL,
    })


@app.route('/api/groups/searches')
def api_pending_searches():
    """API: история поисков."""
    from agent_database import AgentDatabase
    db = AgentDatabase(DB_PATH)
    searches = db.get_all_pending_searches(limit=30)
    return jsonify(searches)


@app.route('/api/groups/pending')
def api_pending_groups():
    """API: список групп в очереди обработки."""
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, username, status, error_message, created_at, processed_at
        FROM pending_groups
        ORDER BY created_at DESC
        LIMIT 50
    """).fetchall()
    conn.close()

    return jsonify([{
        "id": row["id"],
        "username": row["username"],
        "status": row["status"],
        "error_message": row["error_message"],
        "created_at": row["created_at"],
        "processed_at": row["processed_at"],
    } for row in rows])


@app.route('/api/proactive/test', methods=['POST'])
def api_test_proactive():
    """API: запустить тестовую генерацию проактивного поста (БЕЗ отправки)."""
    try:
        # Генерация поста без отправки через subprocess
        result = subprocess.run(
            [sys.executable, "test_proactive.py", "--dry"],
            cwd=app.root_path,
            capture_output=True,
            text=True,
            timeout=60
        )
        return jsonify({
            "success": result.returncode == 0,
            "output": result.stdout,
            "error": result.stderr
        })
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Timeout (60s)"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================
# 👁 VIEWS — boost / toggle / KPI
# ============================================
@app.route('/api/views/stats')
def api_views_stats():
    from agent_database import AgentDatabase
    db = AgentDatabase()
    return jsonify({"views_24h": db.count_views_24h()})


@app.route('/api/agents/<int:agent_id>/views', methods=['POST'])
def api_set_agent_views(agent_id):
    from agent_database import AgentDatabase
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled", False))
    db = AgentDatabase()
    ok = db.set_agent_views_enabled(agent_id, enabled)
    return jsonify({"ok": ok, "agent_id": agent_id, "views_enabled": enabled})


def _ensure_agent_stopped():
    """Возвращает (ok, error_message). Если multi_agent запущен — нельзя крутить
    реакции/views параллельно: pyrogram session-файлы SQLite заблокированы."""
    pidfile = os.path.join(os.path.dirname(DB_PATH) or '.', 'agent.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # alive?
        return False, "Остановите агента (кнопка в шапке) перед ручной накруткой — иначе session-файлы заблокированы и Telegram отбрасывает запросы."
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return True, ""
    except Exception:
        return True, ""


@app.route('/api/reactions/run', methods=['POST'])
def api_reactions_run():
    """Ручная накрутка лайков на последние посты в группе.
    body: {group_id, count?, message_ids?, agent_ids?[], emoji?}"""
    ok, err = _ensure_agent_stopped()
    if not ok:
        return jsonify({"ok": False, "error": err}), 409

    payload = request.get_json(silent=True) or {}
    group_id = int(payload.get("group_id") or 0)
    count = int(payload.get("count") or 10)
    message_ids = payload.get("message_ids") or []
    agent_ids = payload.get("agent_ids") or []
    emoji = (payload.get("emoji") or "").strip()
    if not group_id:
        return jsonify({"ok": False, "error": "group_id required"}), 400
    if count > 50:
        count = 50  # реакции жёстче лимитируем чем views

    args = [sys.executable, "-u", "-m", "reactions_runner",
            "--group-id", str(group_id),
            "--count", str(count)]
    if message_ids: args += ["--msg-ids", ",".join(str(x) for x in message_ids)]
    if agent_ids:   args += ["--agents", ",".join(str(x) for x in agent_ids)]
    if emoji:       args += ["--emoji", emoji]

    log = open(os.path.join(os.path.dirname(DB_PATH) or '.', 'reactions.log'), "ab")
    proc = subprocess.Popen(args, cwd=app.root_path, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    return jsonify({"ok": True, "pid": proc.pid, "group_id": group_id, "count": count})


@app.route('/api/views/run', methods=['POST'])
def api_views_run():
    """Ручная накрутка просмотров.
    body: {group_id, count?, message_ids?, agent_ids?[]}
    Спавнит отдельный процесс который выполняет в asyncio и пишет в interactions."""
    ok, err = _ensure_agent_stopped()
    if not ok:
        return jsonify({"ok": False, "error": err}), 409
    payload = request.get_json(silent=True) or {}
    group_id = int(payload.get("group_id") or 0)
    count = int(payload.get("count") or 20)
    message_ids = payload.get("message_ids") or []
    agent_ids = payload.get("agent_ids") or []

    if not group_id:
        return jsonify({"ok": False, "error": "group_id required"}), 400
    if count > 200:
        count = 200

    import json as _json
    args = [sys.executable, "-u", "-m", "views_runner",
            "--group-id", str(group_id),
            "--count", str(count)]
    if message_ids:
        args += ["--msg-ids", ",".join(str(x) for x in message_ids)]
    if agent_ids:
        args += ["--agents", ",".join(str(x) for x in agent_ids)]

    log = open(_AGENT_LOG_FILE if False else os.path.join(os.path.dirname(DB_PATH) or '.', 'views.log'), "ab")
    proc = subprocess.Popen(
        args, cwd=app.root_path, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return jsonify({"ok": True, "pid": proc.pid, "group_id": group_id, "count": count, "agents": agent_ids or "all-enabled"})


# ============================================
# AGENT PROCESS CONTROL (start/stop multi_agent.py from UI)
# ============================================
_AGENT_PID_FILE = os.path.join(os.path.dirname(DB_PATH) or ".", "agent.pid")
_AGENT_LOG_FILE = os.path.join(os.path.dirname(DB_PATH) or ".", "agent.log")


def _agent_pid_alive() -> int:
    """Return PID if a running agent process exists per pidfile, else 0."""
    try:
        with open(_AGENT_PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return 0


@app.route('/api/agent/status')
def api_agent_status():
    pid = _agent_pid_alive()
    return jsonify({"running": bool(pid), "pid": pid})


@app.route('/api/agent/start', methods=['POST'])
def api_agent_start():
    if _agent_pid_alive():
        return jsonify({"ok": False, "error": "уже запущен"}), 409
    try:
        log = open(_AGENT_LOG_FILE, "ab")
        proc = subprocess.Popen(
            [sys.executable, "-u", "multi_agent.py"],
            cwd=app.root_path,
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(_AGENT_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/agent/stop', methods=['POST'])
def api_agent_stop():
    import signal
    pid = _agent_pid_alive()
    if not pid:
        return jsonify({"ok": False, "error": "не запущен"}), 404
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try: os.kill(pid, signal.SIGTERM)
        except Exception as e: return jsonify({"ok": False, "error": str(e)}), 500
    try: os.remove(_AGENT_PID_FILE)
    except Exception: pass
    return jsonify({"ok": True})


@app.route('/api/agent/log')
def api_agent_log():
    try:
        with open(_AGENT_LOG_FILE) as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read()
        return jsonify({"ok": True, "log": tail})
    except FileNotFoundError:
        return jsonify({"ok": True, "log": ""})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.getenv("PORT", "5001"))
    print("=" * 60)
    print("🎯 MoneyMaker AI-Agent Mini App")
    print("=" * 60)
    print(f"📊 База данных: {DB_PATH}")
    print(f"🔐 Auth: {'DISABLED' if AUTH_DISABLED else f'enabled, {len(ALLOWED_USER_IDS)} user(s) whitelisted'}")
    print(f"🌐 Слушаем :{port}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
