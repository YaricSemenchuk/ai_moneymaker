import sqlite3
import re
import unicodedata
from typing import Optional, List, Dict
from datetime import datetime

# Визуально-идентичные кириллические символы → латиница.
# Спамеры часто мешают раскладки чтобы обойти словарные фильтры.
_HOMOGLYPH_MAP = str.maketrans({
    'а': 'a', 'в': 'b', 'с': 'c', 'е': 'e', 'н': 'h', 'к': 'k',
    'м': 'm', 'о': 'o', 'р': 'p', 'т': 't', 'у': 'y', 'х': 'x',
    'і': 'i', 'ј': 'j', 'ѕ': 's', 'ԁ': 'd', 'ɡ': 'g', 'ӏ': 'l',
})
_ZERO_WIDTH_RE = re.compile(r'[​-‏⁠﻿]')
_WS_RE = re.compile(r'\s+')


def _normalize_for_check(s: str) -> str:
    """Приводит текст к канон. форме: NFKD + lower + homoglyphs + collapse ws."""
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', s).lower()
    s = _ZERO_WIDTH_RE.sub('', s)
    s = s.translate(_HOMOGLYPH_MAP)
    s = _WS_RE.sub(' ', s).strip()
    return s

class AgentDatabase:
    def __init__(self, db_path: str = None):
        if db_path is None:
            import os as _os
            db_path = _os.getenv("DB_PATH", "moneymaker_agent.db")
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        """Initialize database tables for the AI agent."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Agent accounts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_accounts (
                id INTEGER PRIMARY KEY,
                phone_number TEXT UNIQUE NOT NULL,
                session_name TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'inactive',
                proxy_ip TEXT,
                proxy_port INTEGER,
                referral_target TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP
            )
        ''')

        # Миграция: добавляем колонку referral_target если её ещё нет
        try:
            cursor.execute('ALTER TABLE agent_accounts ADD COLUMN referral_target TEXT')
        except sqlite3.OperationalError:
            pass  # уже добавлена

        # Target groups table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS target_groups (
                id INTEGER PRIMARY KEY,
                telegram_group_id INTEGER UNIQUE NOT NULL,
                title TEXT NOT NULL,
                username TEXT,
                description TEXT,
                members_count INTEGER,
                is_public BOOLEAN DEFAULT 1,
                is_channel BOOLEAN DEFAULT 0,
                linked_chat_id INTEGER,
                status TEXT DEFAULT 'discovered',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_monitored TIMESTAMP
            )
        ''')

        # Миграция: добавляем колонки is_channel и linked_chat_id если их ещё нет
        for col_def in [
            ('is_channel', 'ALTER TABLE target_groups ADD COLUMN is_channel BOOLEAN DEFAULT 0'),
            ('linked_chat_id', 'ALTER TABLE target_groups ADD COLUMN linked_chat_id INTEGER'),
            # Назначение группы конкретному агенту (NULL = общий пул).
            ('assigned_agent_id', 'ALTER TABLE target_groups ADD COLUMN assigned_agent_id INTEGER'),
        ]:
            try:
                cursor.execute(col_def[1])
            except sqlite3.OperationalError:
                pass  # уже добавлена

        # Миграция: per-account флаг разрешения реакций (по умолчанию включено)
        try:
            cursor.execute('ALTER TABLE agent_accounts ADD COLUMN reactions_enabled INTEGER DEFAULT 1')
        except sqlite3.OperationalError:
            pass

        # Миграция: per-account флаг авто-просмотров (по умолчанию выключено)
        try:
            cursor.execute('ALTER TABLE agent_accounts ADD COLUMN views_enabled INTEGER DEFAULT 0')
        except sqlite3.OperationalError:
            pass

        # Миграция: source_keyword / source_category для target_groups
        # (что именно нашло группу при scouting — нужно для назначения по категории)
        for col_def in [
            ('source_keyword', 'ALTER TABLE target_groups ADD COLUMN source_keyword TEXT'),
            ('source_category','ALTER TABLE target_groups ADD COLUMN source_category TEXT'),
        ]:
            try: cursor.execute(col_def[1])
            except sqlite3.OperationalError: pass
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_target_groups_category ON target_groups(source_category)')
        except sqlite3.OperationalError: pass

        # Interactions table (logs of messages sent by agents)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                user_id INTEGER,
                message_text TEXT,
                response_text TEXT,
                interaction_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (agent_id) REFERENCES agent_accounts(id),
                FOREIGN KEY (group_id) REFERENCES target_groups(id)
            )
        ''')

        # Миграция: добавляем колонку error_code в interactions
        try:
            cursor.execute('ALTER TABLE interactions ADD COLUMN error_code TEXT')
        except sqlite3.OperationalError:
            pass  # уже добавлена

        # Миграция: per-account proxy + device для антибана/тдата-импорта
        for col_def in [
            ('proxy_url',     'ALTER TABLE agent_accounts ADD COLUMN proxy_url TEXT'),
            ('device_model',  'ALTER TABLE agent_accounts ADD COLUMN device_model TEXT'),
            ('system_version','ALTER TABLE agent_accounts ADD COLUMN system_version TEXT'),
            ('app_version',   'ALTER TABLE agent_accounts ADD COLUMN app_version TEXT'),
        ]:
            try:
                cursor.execute(col_def[1])
            except sqlite3.OperationalError:
                pass

        # Tasks from moneymaker.quest (cached)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY,
                task_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                reward REAL,
                category TEXT,
                referral_link TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Очередь групп для добавления через дашборд
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_groups (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        ''')

        # Уроки из банов: что НЕ говорить чтобы не попасть в бан
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ban_lessons (
                id INTEGER PRIMARY KEY,
                forbidden_phrase TEXT NOT NULL,
                error_type TEXT,
                topic TEXT,
                recommendation TEXT,
                source_group_id INTEGER,
                source_group_title TEXT,
                source_message TEXT,
                ban_count INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                auto_learned INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_triggered_at TIMESTAMP,
                trigger_count INTEGER DEFAULT 0,
                UNIQUE(forbidden_phrase, error_type)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ban_lessons_enabled ON ban_lessons(enabled)')

        # Профили активных пользователей (тех кто писал релевантное)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY,
                telegram_user_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language TEXT,
                total_relevant_messages INTEGER DEFAULT 0,
                avg_interest_level REAL DEFAULT 0,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_message_text TEXT,
                last_group_id INTEGER,
                status TEXT DEFAULT 'new',
                notes TEXT
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_profiles_status ON user_profiles(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_profiles_total ON user_profiles(total_relevant_messages DESC)')

        # История сообщений активных юзеров
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_messages (
                id INTEGER PRIMARY KEY,
                user_profile_id INTEGER NOT NULL,
                group_id INTEGER,
                message_text TEXT NOT NULL,
                interest_level REAL DEFAULT 0,
                intent TEXT,
                language TEXT,
                replied_by_agent_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_profile_id) REFERENCES user_profiles(id),
                FOREIGN KEY (group_id) REFERENCES target_groups(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_messages_user ON user_messages(user_profile_id, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_messages_group ON user_messages(group_id)')

        # Membership: per-agent статус по каждой группе
        # Чтобы при рестарте не дёргать Telegram повторно для тех групп,
        # куда агент уже вступил или отправил заявку
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agent_group_membership (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                status TEXT DEFAULT 'unknown',
                last_attempt TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                FOREIGN KEY (agent_id) REFERENCES agent_accounts(id),
                FOREIGN KEY (group_id) REFERENCES target_groups(id),
                UNIQUE(agent_id, group_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_membership_agent ON agent_group_membership(agent_id, status)')

        # Пул ключевых слов для авто-парсинга (категория + слово)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS keyword_pool (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                keyword TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, keyword)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_keyword_pool_category ON keyword_pool(category)')

        # Очередь поисков по ключевым словам (через дашборд)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_searches (
                id INTEGER PRIMARY KEY,
                keywords TEXT NOT NULL,
                max_results INTEGER DEFAULT 10,
                status TEXT DEFAULT 'pending',
                groups_found INTEGER DEFAULT 0,
                groups_added INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP
            )
        ''')

        # Лог прогонов парсинга групп (кто дежурил, что нашёл)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scout_runs (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP,
                keywords TEXT,
                groups_found INTEGER DEFAULT 0,
                groups_added INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                error_message TEXT,
                FOREIGN KEY (agent_id) REFERENCES agent_accounts(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_scout_runs_started ON scout_runs(started_at DESC)')

        # Proactive posts log (когда агент САМ инициировал диалог)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS proactive_posts (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                post_text TEXT NOT NULL,
                template_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (agent_id) REFERENCES agent_accounts(id),
                FOREIGN KEY (group_id) REFERENCES target_groups(id)
            )
        ''')

        conn.commit()
        conn.close()

        try:
            n = self.dedupe_ban_lessons()
            if n:
                print(f"🧹 Отключено {n} избыточных ban_lessons (перекрыты более короткими)")
        except Exception:
            pass

    def get_connection(self):
        """Get database connection."""
        return sqlite3.connect(self.db_path)

    # --- Agent Accounts ---

    def add_agent_account(self, phone_number: str, session_name: str, proxy_ip: Optional[str] = None, proxy_port: Optional[int] = None) -> int:
        """Add a new agent account."""
        conn = self.get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT INTO agent_accounts (phone_number, session_name, proxy_ip, proxy_port)
                VALUES (?, ?, ?, ?)
            ''', (phone_number, session_name, proxy_ip, proxy_port))
            conn.commit()
            agent_id = cursor.lastrowid
            return agent_id
        except sqlite3.IntegrityError:
            return -1
        finally:
            conn.close()

    def get_agent_account(self, agent_id: int) -> Optional[Dict]:
        """Get agent account by ID."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, phone_number, session_name, status, proxy_ip, proxy_port,
                   created_at, last_active, proxy_url, device_model, system_version, app_version
            FROM agent_accounts WHERE id = ?
        ''', (agent_id,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                'id': row[0],
                'phone_number': row[1],
                'session_name': row[2],
                'status': row[3],
                'proxy_ip': row[4],
                'proxy_port': row[5],
                'created_at': row[6],
                'last_active': row[7],
                'proxy_url': row[8],
                'device_model': row[9],
                'system_version': row[10],
                'app_version': row[11],
            }
        return None

    def update_agent_status(self, agent_id: int, status: str):
        """Update agent account status."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE agent_accounts SET status = ?, last_active = ? WHERE id = ?
        ''', (status, datetime.now(), agent_id))

        conn.commit()
        conn.close()

    def update_agent_referral(self, agent_id: int, referral_target: str):
        """Устанавливает что агент промоутит (бот или группу)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE agent_accounts SET referral_target = ? WHERE id = ?
        ''', (referral_target, agent_id))
        conn.commit()
        conn.close()

    def get_agent_referral(self, agent_id: int) -> Optional[str]:
        """Возвращает что промоутит агент."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT referral_target FROM agent_accounts WHERE id = ?', (agent_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    # --- Target Groups ---

    def add_target_group(self, telegram_group_id: int, title: str, username: Optional[str] = None,
                        description: Optional[str] = None, members_count: Optional[int] = None,
                        is_channel: bool = False, linked_chat_id: Optional[int] = None,
                        source_keyword: Optional[str] = None,
                        source_category: Optional[str] = None) -> int:
        """Add a new target group or channel.
        Если категория не передана а keyword известен — пробуем определить категорию через keyword_pool."""
        if source_keyword and not source_category:
            source_category = self.get_category_for_keyword(source_keyword)

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO target_groups (telegram_group_id, title, username, description,
                                          members_count, is_channel, linked_chat_id,
                                          source_keyword, source_category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (telegram_group_id, title, username, description, members_count,
                  1 if is_channel else 0, linked_chat_id, source_keyword, source_category))
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Группа уже есть — обновим source_keyword/category если они ещё не заполнены
            if source_keyword or source_category:
                try:
                    cursor.execute('''
                        UPDATE target_groups
                        SET source_keyword = COALESCE(source_keyword, ?),
                            source_category = COALESCE(source_category, ?)
                        WHERE telegram_group_id = ?
                    ''', (source_keyword, source_category, telegram_group_id))
                    conn.commit()
                except Exception:
                    pass
            return -1
        finally:
            conn.close()

    def get_category_for_keyword(self, keyword: str) -> Optional[str]:
        """Возвращает категорию из keyword_pool для заданного слова, или None."""
        if not keyword:
            return None
        conn = self.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT category FROM keyword_pool WHERE keyword = ? LIMIT 1", (keyword.strip(),))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    def get_groups_by_statuses(self, statuses: List[str], limit: int = 200) -> List[Dict]:
        """Получает группы по нескольким статусам сразу (например ['joined','active'])."""
        conn = self.get_connection()
        cursor = conn.cursor()

        placeholders = ",".join("?" * len(statuses))
        cursor.execute(f'''
            SELECT id, telegram_group_id, title, username, description, members_count, status, added_at,
                   COALESCE(is_channel, 0), linked_chat_id
            FROM target_groups WHERE status IN ({placeholders}) LIMIT ?
        ''', (*statuses, limit))

        rows = cursor.fetchall()
        conn.close()

        return [{
            'id': row[0], 'telegram_group_id': row[1], 'title': row[2],
            'username': row[3], 'description': row[4], 'members_count': row[5],
            'status': row[6], 'added_at': row[7],
            'is_channel': bool(row[8]), 'linked_chat_id': row[9]
        } for row in rows]

    def get_target_groups(self, status: str = 'discovered', limit: int = 100) -> List[Dict]:
        """Get target groups by status."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, telegram_group_id, title, username, description, members_count, status, added_at,
                   COALESCE(is_channel, 0), linked_chat_id
            FROM target_groups WHERE status = ? LIMIT ?
        ''', (status, limit))

        rows = cursor.fetchall()
        conn.close()

        groups = []
        for row in rows:
            groups.append({
                'id': row[0],
                'telegram_group_id': row[1],
                'title': row[2],
                'username': row[3],
                'description': row[4],
                'members_count': row[5],
                'status': row[6],
                'added_at': row[7],
                'is_channel': bool(row[8]),
                'linked_chat_id': row[9],
            })
        return groups

    def update_group_status(self, group_id: int, status: str):
        """Update group status."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE target_groups SET status = ?, last_monitored = ? WHERE id = ?
        ''', (status, datetime.now(), group_id))

        conn.commit()
        conn.close()

    def count_recent_consecutive_failures(self, group_id: int, lookback: int = 10) -> int:
        """Сколько последних N взаимодействий в группе были failed подряд."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT status FROM interactions
            WHERE group_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        ''', (group_id, lookback))
        rows = cursor.fetchall()
        conn.close()

        n = 0
        for (status,) in rows:
            if status == 'failed':
                n += 1
            else:
                break
        return n

    # --- Interactions ---

    def log_interaction(self, agent_id: int, group_id: int, message_text: str,
                       user_id: Optional[int] = None, response_text: Optional[str] = None,
                       status: str = 'sent', error_code: Optional[str] = None) -> int:
        """Log an interaction (message sent by agent).

        Args:
            status: 'sent' / 'failed' / 'pending' / 'blocked_filter'
            error_code: краткий код причины (CHAT_WRITE_FORBIDDEN, SLOWMODE_WAIT,
                       FLOOD_WAIT, USER_BANNED, FILTER_BLOCK, EMPTY, EXCEPTION, ...)
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO interactions (agent_id, group_id, user_id, message_text, response_text, interaction_type, status, error_code)
            VALUES (?, ?, ?, ?, ?, 'outbound', ?, ?)
        ''', (agent_id, group_id, user_id, message_text, response_text, status, error_code))

        conn.commit()
        interaction_id = cursor.lastrowid
        conn.close()
        return interaction_id

    def update_interaction_status(self, interaction_id: int, status: str):
        """Обновляет статус взаимодействия (sent/failed)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE interactions SET status = ? WHERE id = ?
        ''', (status, interaction_id))
        conn.commit()
        conn.close()

    def get_interactions(self, agent_id: Optional[int] = None, group_id: Optional[int] = None, 
                        limit: int = 100) -> List[Dict]:
        """Get interactions (logs)."""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = "SELECT id, agent_id, group_id, user_id, message_text, response_text, interaction_type, status, created_at FROM interactions"
        params = []

        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        if group_id:
            query += " AND group_id = ?"
            params.append(group_id)

        query += " LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        interactions = []
        for row in rows:
            interactions.append({
                'id': row[0],
                'agent_id': row[1],
                'group_id': row[2],
                'user_id': row[3],
                'message_text': row[4],
                'response_text': row[5],
                'interaction_type': row[6],
                'status': row[7],
                'created_at': row[8]
            })
        return interactions

    # --- Tasks ---

    def add_or_update_task(self, task_id: str, title: str, description: Optional[str] = None, 
                          reward: Optional[float] = None, category: Optional[str] = None, 
                          referral_link: Optional[str] = None):
        """Add or update a task from moneymaker.quest."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR REPLACE INTO tasks (task_id, title, description, reward, category, referral_link, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (task_id, title, description, reward, category, referral_link, datetime.now()))

        conn.commit()
        conn.close()

    # --- Pending Groups Queue ---

    def add_pending_group(self, username: str) -> int:
        """Добавляет группу в очередь на обработку."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO pending_groups (username, status)
            VALUES (?, 'pending')
        ''', (username,))

        conn.commit()
        pid = cursor.lastrowid
        conn.close()
        return pid

    def get_pending_groups(self) -> List[Dict]:
        """Получает все группы со статусом 'pending'."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, username, status, error_message, created_at
            FROM pending_groups
            WHERE status = 'pending'
            ORDER BY created_at ASC
        ''')

        rows = cursor.fetchall()
        conn.close()

        return [{
            'id': row[0],
            'username': row[1],
            'status': row[2],
            'error_message': row[3],
            'created_at': row[4],
        } for row in rows]

    def mark_pending_group_done(self, pid: int, status: str, error: Optional[str] = None):
        """Обновляет статус задачи в очереди."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE pending_groups
            SET status = ?, error_message = ?, processed_at = ?
            WHERE id = ?
        ''', (status, error, datetime.now(), pid))

        conn.commit()
        conn.close()

    # --- Ban Lessons (Learned Rules) ---

    def add_ban_lesson(self, forbidden_phrase: str, error_type: str = None,
                      topic: str = None, recommendation: str = None,
                      source_group_id: int = None, source_group_title: str = None,
                      source_message: str = None, auto_learned: bool = True) -> int:
        """Добавляет/обновляет правило-урок (что не говорить).

        Dedup: если уже существует более короткое включённое правило, чьё
        нормализованное представление целиком содержится в новой фразе,
        новая фраза избыточна (короткое правило её уже ловит) — пропускаем,
        возвращаем id перекрывающего правила.
        """
        norm_new = _normalize_for_check(forbidden_phrase)
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT id, forbidden_phrase FROM ban_lessons WHERE enabled = 1'
            )
            for existing_id, existing_phrase in cursor.fetchall():
                norm_existing = _normalize_for_check(existing_phrase or '')
                if (norm_existing and norm_existing != norm_new
                        and len(norm_existing) < len(norm_new)
                        and norm_existing in norm_new):
                    return existing_id
                # Обратная сторона: новая фраза короче и уже покрывает старую —
                # отключаем старую как избыточную.
                if (norm_existing and norm_existing != norm_new
                        and len(norm_new) < len(norm_existing)
                        and norm_new in norm_existing):
                    cursor.execute(
                        'UPDATE ban_lessons SET enabled = 0 WHERE id = ?',
                        (existing_id,)
                    )

            cursor.execute('''
                INSERT INTO ban_lessons
                (forbidden_phrase, error_type, topic, recommendation,
                 source_group_id, source_group_title, source_message, auto_learned)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(forbidden_phrase, error_type) DO UPDATE SET
                    ban_count = ban_count + 1,
                    last_triggered_at = CURRENT_TIMESTAMP
            ''', (forbidden_phrase.lower(), error_type, topic, recommendation,
                  source_group_id, source_group_title,
                  (source_message or '')[:300], 1 if auto_learned else 0))
            conn.commit()
            cursor.execute('SELECT id FROM ban_lessons WHERE forbidden_phrase = ? AND error_type IS ?',
                          (forbidden_phrase.lower(), error_type))
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def dedupe_ban_lessons(self) -> int:
        """Одноразовая чистка: отключает правила, перекрытые более короткими.

        Возвращает число отключённых правил. Безопасно вызывать многократно.
        """
        lessons = self.get_ban_lessons(only_enabled=True)
        norm_map = [
            (l['id'], _normalize_for_check(l.get('forbidden_phrase') or ''))
            for l in lessons
        ]
        norm_map = [(i, p) for i, p in norm_map if p]
        to_disable = set()
        for i, (id_a, phrase_a) in enumerate(norm_map):
            for id_b, phrase_b in norm_map:
                if id_a == id_b or id_b in to_disable:
                    continue
                if len(phrase_b) < len(phrase_a) and phrase_b in phrase_a:
                    to_disable.add(id_a)
                    break
        if not to_disable:
            return 0
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.executemany(
            'UPDATE ban_lessons SET enabled = 0 WHERE id = ?',
            [(lid,) for lid in to_disable]
        )
        conn.commit()
        conn.close()
        return len(to_disable)

    def get_ban_lessons(self, only_enabled: bool = True) -> List[Dict]:
        """Получает все правила."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if only_enabled:
            cursor.execute('SELECT * FROM ban_lessons WHERE enabled = 1 ORDER BY ban_count DESC, last_triggered_at DESC')
        else:
            cursor.execute('SELECT * FROM ban_lessons ORDER BY ban_count DESC, last_triggered_at DESC')
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        conn.close()
        return [dict(zip(cols, r)) for r in rows]

    def update_ban_lesson(self, lid: int, **kwargs) -> bool:
        """Обновление правила. Принимает: enabled, recommendation, forbidden_phrase, topic."""
        if not kwargs:
            return False
        valid_fields = ['enabled', 'recommendation', 'forbidden_phrase', 'topic', 'error_type']
        updates = []
        params = []
        for k, v in kwargs.items():
            if k in valid_fields:
                updates.append(f"{k} = ?")
                params.append(int(v) if k == 'enabled' else v)
        if not updates:
            return False

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            params.append(lid)
            cursor.execute(f"UPDATE ban_lessons SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            return False
        finally:
            conn.close()

    def delete_ban_lesson(self, lid: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ban_lessons WHERE id = ?", (lid,))
        conn.commit()
        ok = cursor.rowcount > 0
        conn.close()
        return ok

    def check_text_against_lessons(self, text: str) -> List[Dict]:
        """Проверяет текст против включённых правил.

        Нормализует обе стороны (homoglyphs, NFKD, lower, zero-width strip)
        и матчит по границам слов — чтобы "scam" не ловил "scammer warning",
        но ловил "СКАМ", "sсам" (mixed-script), "s c a m".
        """
        if not text:
            return []
        norm_text = _normalize_for_check(text)
        lessons = self.get_ban_lessons(only_enabled=True)
        matched = []
        for lesson in lessons:
            phrase = _normalize_for_check(lesson.get('forbidden_phrase') or '')
            if not phrase:
                continue
            # Между каждым символом фразы допускаем до 2 непословных
            # разделителей (ловит "s c a m", "s.c.a.m", но не случайные
            # подстроки). Границы слов исключают "scam" внутри "scammer".
            try:
                inner = r'[\W_]{0,3}'.join(re.escape(c) for c in phrase)
                pattern = r'(?<!\w)' + inner + r'(?!\w)'
                if re.search(pattern, norm_text, flags=re.UNICODE):
                    matched.append(lesson)
            except re.error:
                pass
        return matched

    def increment_lesson_trigger(self, lid: int):
        """Увеличивает счётчик срабатываний правила."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE ban_lessons
            SET trigger_count = trigger_count + 1, last_triggered_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (lid,))
        conn.commit()
        conn.close()

    # --- User Profiles (Active Users Tracking) ---

    def upsert_user_profile(self, telegram_user_id: int, username: Optional[str] = None,
                           first_name: Optional[str] = None, last_name: Optional[str] = None,
                           language: Optional[str] = None) -> int:
        """Создаёт или обновляет профиль пользователя. Возвращает user_profile_id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO user_profiles (telegram_user_id, username, first_name, last_name, language)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, username),
                    first_name = COALESCE(excluded.first_name, first_name),
                    last_name = COALESCE(excluded.last_name, last_name),
                    language = COALESCE(excluded.language, language),
                    last_seen_at = CURRENT_TIMESTAMP
            ''', (telegram_user_id, username, first_name, last_name, language))
            conn.commit()

            # Получаем id
            cursor.execute("SELECT id FROM user_profiles WHERE telegram_user_id = ?", (telegram_user_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def add_user_message(self, user_profile_id: int, group_id: int, message_text: str,
                        interest_level: float, intent: Optional[str] = None,
                        language: Optional[str] = None,
                        replied_by_agent_id: Optional[int] = None) -> int:
        """Сохраняет сообщение пользователя + обновляет статистику профиля."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Записываем сообщение
            cursor.execute('''
                INSERT INTO user_messages (user_profile_id, group_id, message_text, interest_level,
                                          intent, language, replied_by_agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_profile_id, group_id, message_text, interest_level, intent, language, replied_by_agent_id))
            msg_id = cursor.lastrowid

            # Обновляем профиль: счётчик + средний интерес + последнее сообщение
            cursor.execute('''
                UPDATE user_profiles SET
                    total_relevant_messages = total_relevant_messages + 1,
                    avg_interest_level = (
                        (avg_interest_level * total_relevant_messages + ?) /
                        (total_relevant_messages + 1)
                    ),
                    last_seen_at = CURRENT_TIMESTAMP,
                    last_message_text = ?,
                    last_group_id = ?
                WHERE id = ?
            ''', (interest_level, message_text[:500], group_id, user_profile_id))

            conn.commit()
            return msg_id
        finally:
            conn.close()

    def update_user_status(self, user_profile_id: int, status: str, notes: Optional[str] = None):
        """Обновляет статус юзера: new/engaged/converted/blocked."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if notes is not None:
                cursor.execute("UPDATE user_profiles SET status = ?, notes = ? WHERE id = ?",
                              (status, notes, user_profile_id))
            else:
                cursor.execute("UPDATE user_profiles SET status = ? WHERE id = ?",
                              (status, user_profile_id))
            conn.commit()
        finally:
            conn.close()

    def get_user_profiles(self, limit: int = 100, status: Optional[str] = None,
                         min_messages: int = 0) -> List[Dict]:
        """Список профилей с фильтрами."""
        conn = self.get_connection()
        cursor = conn.cursor()

        query = '''
            SELECT u.id, u.telegram_user_id, u.username, u.first_name, u.last_name, u.language,
                   u.total_relevant_messages, u.avg_interest_level, u.first_seen_at, u.last_seen_at,
                   u.last_message_text, u.last_group_id, u.status, u.notes,
                   g.title as last_group_title, g.username as last_group_username,
                   COUNT(DISTINCT um.group_id) as groups_count
            FROM user_profiles u
            LEFT JOIN target_groups g ON u.last_group_id = g.id
            LEFT JOIN user_messages um ON u.id = um.user_profile_id
            WHERE u.total_relevant_messages >= ?
        '''
        params = [min_messages]

        if status:
            query += " AND u.status = ?"
            params.append(status)

        query += " GROUP BY u.id ORDER BY u.last_seen_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [{
            'id': r[0], 'telegram_user_id': r[1], 'username': r[2],
            'first_name': r[3], 'last_name': r[4], 'language': r[5],
            'total_relevant_messages': r[6], 'avg_interest_level': r[7],
            'first_seen_at': r[8], 'last_seen_at': r[9],
            'last_message_text': r[10], 'last_group_id': r[11],
            'status': r[12], 'notes': r[13],
            'last_group_title': r[14], 'last_group_username': r[15],
            'groups_count': r[16],
        } for r in rows]

    def get_user_messages(self, user_profile_id: int, limit: int = 50) -> List[Dict]:
        """История сообщений конкретного юзера."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT um.id, um.group_id, um.message_text, um.interest_level, um.intent,
                   um.language, um.replied_by_agent_id, um.created_at,
                   g.title as group_title, g.username as group_username
            FROM user_messages um
            LEFT JOIN target_groups g ON um.group_id = g.id
            WHERE um.user_profile_id = ?
            ORDER BY um.created_at DESC LIMIT ?
        ''', (user_profile_id, limit))
        rows = cursor.fetchall()
        conn.close()

        return [{
            'id': r[0], 'group_id': r[1], 'message_text': r[2],
            'interest_level': r[3], 'intent': r[4], 'language': r[5],
            'replied_by_agent_id': r[6], 'created_at': r[7],
            'group_title': r[8], 'group_username': r[9],
        } for r in rows]

    def get_users_stats(self) -> Dict:
        """Общая статистика по юзерам."""
        conn = self.get_connection()
        cursor = conn.cursor()

        result = {}
        cursor.execute("SELECT COUNT(*) FROM user_profiles")
        result['total_users'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM user_messages")
        result['total_messages'] = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM user_profiles WHERE date(last_seen_at) = date('now')")
        result['today_users'] = cursor.fetchone()[0]

        cursor.execute("SELECT status, COUNT(*) FROM user_profiles GROUP BY status")
        result['by_status'] = {row[0]: row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT AVG(avg_interest_level) FROM user_profiles WHERE total_relevant_messages > 0")
        result['avg_interest'] = cursor.fetchone()[0] or 0

        conn.close()
        return result

    # --- Agent Group Membership ---

    def set_membership(self, agent_id: int, group_id: int, status: str,
                      error_message: Optional[str] = None) -> None:
        """Устанавливает статус: куда агент вступил / куда отправил заявку / куда не получилось.

        Args:
            status: 'joined', 'requested', 'failed', 'banned', 'left'
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO agent_group_membership (agent_id, group_id, status, last_attempt, error_message)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, group_id) DO UPDATE SET
                    status = excluded.status,
                    last_attempt = excluded.last_attempt,
                    error_message = excluded.error_message
            ''', (agent_id, group_id, status, datetime.now(), error_message))
            conn.commit()
        finally:
            conn.close()

    def get_membership(self, agent_id: int, group_id: int) -> Optional[Dict]:
        """Возвращает запись о membership агента в группе."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, agent_id, group_id, status, last_attempt, error_message
            FROM agent_group_membership
            WHERE agent_id = ? AND group_id = ?
        ''', (agent_id, group_id))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'id': row[0], 'agent_id': row[1], 'group_id': row[2],
                'status': row[3], 'last_attempt': row[4], 'error_message': row[5]
            }
        return None

    def blacklist_group(self, group_id: int, reason: Optional[str] = None) -> None:
        """Помечает группу как banned глобально и для всех агентов.

        После вызова:
        - target_groups.status = 'banned' (исключается из всех scout/listen/proactive выборок)
        - agent_group_membership.status = 'banned' для всех агентов (на случай membership-фильтров)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE target_groups SET status = 'banned', last_monitored = ? WHERE id = ?",
                (datetime.now(), group_id),
            )
            cursor.execute(
                """
                UPDATE agent_group_membership
                   SET status = 'banned',
                       last_attempt = ?,
                       error_message = COALESCE(?, error_message)
                 WHERE group_id = ?
                """,
                (datetime.now(), reason, group_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_agent_memberships(self, agent_id: int, statuses: List[str] = None) -> List[Dict]:
        """Возвращает все записи membership для агента."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            cursor.execute(f'''
                SELECT agent_id, group_id, status, last_attempt
                FROM agent_group_membership
                WHERE agent_id = ? AND status IN ({placeholders})
            ''', (agent_id, *statuses))
        else:
            cursor.execute('''
                SELECT agent_id, group_id, status, last_attempt
                FROM agent_group_membership
                WHERE agent_id = ?
            ''', (agent_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'agent_id': r[0], 'group_id': r[1], 'status': r[2], 'last_attempt': r[3]} for r in rows]

    def filter_groups_to_join(self, agent_id: int, groups: List[Dict],
                              skip_statuses: List[str] = None) -> List[Dict]:
        """
        Фильтрует список групп, исключая те куда агент уже:
        - вступил (joined)
        - отправил заявку (requested)
        - не имеет прав (failed, banned)

        Args:
            groups: список групп из get_target_groups
            skip_statuses: какие membership-статусы пропускать (по умолчанию все кроме unknown/left)
        """
        if skip_statuses is None:
            # По умолчанию НЕ пытаемся снова если уже:
            skip_statuses = ['joined', 'requested', 'failed', 'banned']

        memberships = self.get_agent_memberships(agent_id)
        skip_group_ids = {m['group_id'] for m in memberships if m['status'] in skip_statuses}

        return [g for g in groups if g['id'] not in skip_group_ids]

    def count_memberships_by_status(self, agent_id: int) -> Dict[str, int]:
        """Сколько у агента joined/requested/failed."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT status, COUNT(*) FROM agent_group_membership
            WHERE agent_id = ? GROUP BY status
        ''', (agent_id,))
        rows = cursor.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}

    # --- Keyword Pool ---

    def seed_keyword_pool(self, pool: Dict[str, List[str]]):
        """Заполняет таблицу из конфига если она пустая."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM keyword_pool")
        count = cursor.fetchone()[0]

        if count == 0:
            for category, words in pool.items():
                for word in words:
                    try:
                        cursor.execute(
                            "INSERT INTO keyword_pool (category, keyword) VALUES (?, ?)",
                            (category, word)
                        )
                    except sqlite3.IntegrityError:
                        pass
            conn.commit()
        conn.close()

    def get_all_keywords(self, only_enabled: bool = True) -> List[Dict]:
        """Получает все слова из пула."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if only_enabled:
            cursor.execute("SELECT id, category, keyword, enabled FROM keyword_pool WHERE enabled = 1 ORDER BY category, keyword")
        else:
            cursor.execute("SELECT id, category, keyword, enabled FROM keyword_pool ORDER BY category, keyword")
        rows = cursor.fetchall()
        conn.close()
        return [{"id": r[0], "category": r[1], "keyword": r[2], "enabled": bool(r[3])} for r in rows]

    def get_keywords_by_category(self) -> Dict[str, List[str]]:
        """Группирует enabled-слова по категориям (для scouting)."""
        keywords = self.get_all_keywords(only_enabled=True)
        result = {}
        for kw in keywords:
            cat = kw["category"]
            if cat not in result:
                result[cat] = []
            result[cat].append(kw["keyword"])
        return result

    def add_keyword(self, category: str, keyword: str) -> int:
        """Добавляет новое слово."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO keyword_pool (category, keyword) VALUES (?, ?)",
                (category.strip(), keyword.strip())
            )
            conn.commit()
            kid = cursor.lastrowid
            return kid
        except sqlite3.IntegrityError:
            return -1
        finally:
            conn.close()

    def update_keyword(self, kid: int, keyword: Optional[str] = None,
                       enabled: Optional[bool] = None,
                       category: Optional[str] = None) -> bool:
        """Обновляет слово (текст / enabled / категорию)."""
        conn = self.get_connection()
        cursor = conn.cursor()

        updates = []
        params = []
        if keyword is not None:
            updates.append("keyword = ?")
            params.append(keyword.strip())
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if category is not None:
            updates.append("category = ?")
            params.append(category.strip())

        if not updates:
            conn.close()
            return False

        params.append(kid)
        try:
            cursor.execute(f"UPDATE keyword_pool SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()
            return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def delete_keyword(self, kid: int) -> bool:
        """Удаляет слово."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM keyword_pool WHERE id = ?", (kid,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected > 0

    def rename_category(self, old_name: str, new_name: str) -> int:
        """Переименовывает категорию во всех словах."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE keyword_pool SET category = ? WHERE category = ?",
                           (new_name.strip(), old_name.strip()))
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete_category(self, category: str) -> int:
        """Удаляет всю категорию."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM keyword_pool WHERE category = ?", (category,))
        conn.commit()
        affected = cursor.rowcount
        conn.close()
        return affected

    # --- Pending Searches Queue ---

    def add_pending_search(self, keywords: str, max_results: int = 10) -> int:
        """Добавляет поиск в очередь."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO pending_searches (keywords, max_results, status)
            VALUES (?, ?, 'pending')
        ''', (keywords, max_results))
        conn.commit()
        sid = cursor.lastrowid
        conn.close()
        return sid

    def get_pending_searches(self) -> List[Dict]:
        """Получает все поиски со статусом 'pending'."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, keywords, max_results, status, groups_found, groups_added, error_message, created_at
            FROM pending_searches WHERE status = 'pending'
            ORDER BY created_at ASC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [{
            'id': r[0], 'keywords': r[1], 'max_results': r[2],
            'status': r[3], 'groups_found': r[4], 'groups_added': r[5],
            'error_message': r[6], 'created_at': r[7],
        } for r in rows]

    def get_all_pending_searches(self, limit: int = 50) -> List[Dict]:
        """Получает историю поисков (любого статуса)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, keywords, max_results, status, groups_found, groups_added, error_message, created_at, processed_at
            FROM pending_searches
            ORDER BY created_at DESC LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [{
            'id': r[0], 'keywords': r[1], 'max_results': r[2],
            'status': r[3], 'groups_found': r[4], 'groups_added': r[5],
            'error_message': r[6], 'created_at': r[7], 'processed_at': r[8],
        } for r in rows]

    def mark_search_done(self, sid: int, status: str, found: int = 0, added: int = 0, error: Optional[str] = None):
        """Обновляет статус поиска."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE pending_searches
            SET status = ?, groups_found = ?, groups_added = ?, error_message = ?, processed_at = ?
            WHERE id = ?
        ''', (status, found, added, error, datetime.now(), sid))
        conn.commit()
        conn.close()

    def get_pending_group_status(self, pid: int) -> Optional[Dict]:
        """Получает статус конкретной задачи."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, username, status, error_message, created_at, processed_at
            FROM pending_groups WHERE id = ?
        ''', (pid,))

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                'id': row[0],
                'username': row[1],
                'status': row[2],
                'error_message': row[3],
                'created_at': row[4],
                'processed_at': row[5],
            }
        return None

    # --- Proactive Posts ---

    def log_proactive_post(self, agent_id: int, group_id: int, post_text: str,
                          template_used: Optional[str] = None) -> int:
        """Логирует проактивный пост."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            INSERT INTO proactive_posts (agent_id, group_id, post_text, template_used)
            VALUES (?, ?, ?, ?)
        ''', (agent_id, group_id, post_text, template_used))

        conn.commit()
        post_id = cursor.lastrowid
        conn.close()
        return post_id

    def get_last_proactive_post_time(self, group_id: int) -> Optional[datetime]:
        """Возвращает время последнего проактивного поста в группе."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT created_at FROM proactive_posts
            WHERE group_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (group_id,))

        row = cursor.fetchone()
        conn.close()

        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except (ValueError, TypeError):
                return None
        return None

    def count_proactive_posts_today(self, agent_id: int) -> int:
        """Считает количество проактивных постов агента за сегодня."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT COUNT(*) FROM proactive_posts
            WHERE agent_id = ?
            AND date(created_at) = date('now')
        ''', (agent_id,))

        count = cursor.fetchone()[0]
        conn.close()
        return count or 0

    def count_proactive_posts_today_in_group(self, agent_id: int, group_id: int) -> int:
        """Сколько постов агент сделал в эту группу за последние 24 часа."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM proactive_posts
            WHERE agent_id = ? AND group_id = ?
              AND created_at > datetime('now', '-24 hours')
        ''', (agent_id, group_id))
        count = cursor.fetchone()[0]
        conn.close()
        return count or 0

    # --- Group ↔ Agent assignment ---

    def assign_group(self, group_id: int, agent_id: Optional[int]) -> bool:
        """Назначает группу агенту (None = вернуть в общий пул)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE target_groups SET assigned_agent_id = ? WHERE id = ?',
            (agent_id, group_id)
        )
        conn.commit()
        ok = cursor.rowcount > 0
        conn.close()
        return ok

    def get_assigned_group_ids(self, agent_id: int) -> List[int]:
        """Возвращает id групп, назначенных агенту."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT id FROM target_groups WHERE assigned_agent_id = ?',
            (agent_id,)
        )
        rows = [r[0] for r in cursor.fetchall()]
        conn.close()
        return rows

    def get_groups_with_assignment(self, statuses: Optional[List[str]] = None) -> List[Dict]:
        """Список всех групп с полем assigned_agent_id (для UI)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        if statuses:
            placeholders = ','.join('?' * len(statuses))
            cursor.execute(f'''
                SELECT id, telegram_group_id, title, username, members_count,
                       status, assigned_agent_id
                FROM target_groups
                WHERE status IN ({placeholders})
                ORDER BY (assigned_agent_id IS NULL), assigned_agent_id, title
            ''', statuses)
        else:
            cursor.execute('''
                SELECT id, telegram_group_id, title, username, members_count,
                       status, assigned_agent_id
                FROM target_groups
                ORDER BY (assigned_agent_id IS NULL), assigned_agent_id, title
            ''')
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
        conn.close()
        return rows

    def auto_distribute_groups(self, agent_ids: List[int],
                               statuses: Optional[List[str]] = None,
                               only_unassigned: bool = False) -> int:
        """Round-robin распределение групп между агентами.

        Args:
            agent_ids: список agent_id куда распределять.
            statuses: какие группы брать (по умолчанию joined/active).
            only_unassigned: если True — только NULL-группы, иначе все.

        Returns: число назначений.
        """
        if not agent_ids:
            return 0
        statuses = statuses or ['joined', 'active']
        placeholders = ','.join('?' * len(statuses))
        conn = self.get_connection()
        cursor = conn.cursor()
        where_extra = ' AND assigned_agent_id IS NULL' if only_unassigned else ''
        cursor.execute(f'''
            SELECT id FROM target_groups
            WHERE status IN ({placeholders}){where_extra}
            ORDER BY id
        ''', statuses)
        group_ids = [r[0] for r in cursor.fetchall()]
        n = 0
        for i, gid in enumerate(group_ids):
            aid = agent_ids[i % len(agent_ids)]
            cursor.execute(
                'UPDATE target_groups SET assigned_agent_id = ? WHERE id = ?',
                (aid, gid)
            )
            n += 1
        conn.commit()
        conn.close()
        return n

    def get_agent_reactions_enabled(self, agent_id: int) -> bool:
        """Разрешены ли реакции для агента (по умолчанию True)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT reactions_enabled FROM agent_accounts WHERE id = ?',
            (agent_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row is None:
            return True
        return bool(row[0]) if row[0] is not None else True

    def get_agent_views_enabled(self, agent_id: int) -> bool:
        """Разрешены ли авто-просмотры для агента (по умолчанию False)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT views_enabled FROM agent_accounts WHERE id = ?', (agent_id,))
        row = cursor.fetchone()
        conn.close()
        return bool(row[0]) if row and row[0] is not None else False

    def set_agent_views_enabled(self, agent_id: int, enabled: bool) -> bool:
        """Включает/выключает авто-просмотры для агента."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE agent_accounts SET views_enabled = ? WHERE id = ?', (1 if enabled else 0, agent_id))
        ok = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return ok

    def log_views(self, agent_id: int, group_id: int, count: int, status: str = 'ok'):
        """Логирует пачку просмотров как одну запись в interactions."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO interactions (agent_id, group_id, interaction_type, status, response_text)
            VALUES (?, ?, 'view', ?, ?)
        ''', (agent_id, group_id, status, f'+{count} views'))
        conn.commit()
        conn.close()

    def count_views_24h(self) -> int:
        """Сумма просмотров за последние 24ч (для KPI)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(CAST(SUBSTR(response_text, 2, INSTR(response_text, ' ')-2) AS INTEGER)), 0)
            FROM interactions
            WHERE interaction_type='view' AND status='ok'
              AND created_at >= datetime('now', '-1 day')
              AND response_text LIKE '+%'
        """)
        n = cursor.fetchone()[0] or 0
        conn.close()
        return int(n)

    # --- Scout Runs (история парсинга) ---

    def start_scout_run(self, agent_id: int, keywords: List[str]) -> int:
        """Записывает начало прогона. Возвращает id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO scout_runs (agent_id, keywords, status) VALUES (?, ?, ?)',
            (agent_id, ', '.join(keywords or []), 'running')
        )
        conn.commit()
        rid = cursor.lastrowid
        conn.close()
        return rid

    def finish_scout_run(self, run_id: int, groups_found: int,
                         groups_added: int, status: str = 'done',
                         error_message: Optional[str] = None):
        """Закрывает прогон с итогами."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE scout_runs
            SET finished_at = CURRENT_TIMESTAMP,
                groups_found = ?, groups_added = ?,
                status = ?, error_message = ?
            WHERE id = ?
        ''', (groups_found, groups_added, status, error_message, run_id))
        conn.commit()
        conn.close()

    def get_scout_runs(self, limit: int = 50) -> List[Dict]:
        """Последние прогоны парсинга с именем агента."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT s.id, s.agent_id, a.phone_number, s.started_at, s.finished_at,
                   s.keywords, s.groups_found, s.groups_added, s.status,
                   s.error_message
            FROM scout_runs s
            LEFT JOIN agent_accounts a ON a.id = s.agent_id
            ORDER BY s.started_at DESC
            LIMIT ?
        ''', (limit,))
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]
        conn.close()
        return rows

    def set_agent_reactions_enabled(self, agent_id: int, enabled: bool) -> bool:
        """Включает/выключает реакции для агента."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE agent_accounts SET reactions_enabled = ? WHERE id = ?',
            (1 if enabled else 0, agent_id)
        )
        conn.commit()
        ok = cursor.rowcount > 0
        conn.close()
        return ok

    def get_proactive_posts(self, limit: int = 50) -> List[Dict]:
        """Получает историю проактивных постов."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT p.id, p.agent_id, p.group_id, p.post_text, p.template_used, p.created_at,
                   g.title as group_title, g.username as group_username
            FROM proactive_posts p
            LEFT JOIN target_groups g ON p.group_id = g.id
            ORDER BY p.created_at DESC LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [{
            'id': row[0],
            'agent_id': row[1],
            'group_id': row[2],
            'post_text': row[3],
            'template_used': row[4],
            'created_at': row[5],
            'group_title': row[6],
            'group_username': row[7],
        } for row in rows]

    # --- Tasks ---

    def get_tasks(self, category: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get tasks."""
        conn = self.get_connection()
        cursor = conn.cursor()

        if category:
            cursor.execute('''
                SELECT id, task_id, title, description, reward, category, referral_link
                FROM tasks WHERE category = ? LIMIT ?
            ''', (category, limit))
        else:
            cursor.execute('''
                SELECT id, task_id, title, description, reward, category, referral_link
                FROM tasks LIMIT ?
            ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        tasks = []
        for row in rows:
            tasks.append({
                'id': row[0],
                'task_id': row[1],
                'title': row[2],
                'description': row[3],
                'reward': row[4],
                'category': row[5],
                'referral_link': row[6]
            })
        return tasks
