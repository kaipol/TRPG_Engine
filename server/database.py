"""Central SQLite wiring and core schema initialization for Z.R.I.C."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable
import sqlite3


_db_file: str = ""


def configure_database(db_file: str) -> None:
    """Configure the shared SQLite database path used by the main app."""
    global _db_file
    _db_file = db_file


def get_db_connection() -> sqlite3.Connection:
    """Create a SQLite connection with the app's concurrency pragmas."""
    if not _db_file:
        raise RuntimeError("Database path has not been configured")
    conn = sqlite3.connect(_db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def safe_db():
    """Open a DB connection and rollback on errors."""
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(
    *,
    init_map_tables: Callable[[], None],
    init_rag_tables: Callable[[], None],
) -> None:
    """Create and migrate core Z.R.I.C tables, plus injected module tables."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, summary TEXT, content TEXT, scene_image TEXT DEFAULT '')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS options (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, text TEXT, next_node_id INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS characters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        role TEXT,
        hp INTEGER,
        san INTEGER,
        inventory TEXT DEFAULT '',
        personality TEXT DEFAULT '',
        role_brief TEXT DEFAULT '',
        script_brief TEXT DEFAULT '',
        opening_prompt TEXT DEFAULT '',
        status TEXT DEFAULT 'active'
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS ai_response_cache (
        cache_key          TEXT PRIMARY KEY,
        provider_id        TEXT NOT NULL DEFAULT '',
        model              TEXT NOT NULL DEFAULT '',
        token_policy_mode  TEXT NOT NULL DEFAULT '',
        temperature        REAL NOT NULL DEFAULT 0,
        json_mode          INTEGER NOT NULL DEFAULT 0,
        max_tokens         INTEGER NOT NULL DEFAULT 0,
        system_hash        TEXT NOT NULL DEFAULT '',
        user_hash          TEXT NOT NULL DEFAULT '',
        response_text      TEXT NOT NULL,
        prompt_chars       INTEGER NOT NULL DEFAULT 0,
        response_chars     INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        last_used_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        hit_count          INTEGER NOT NULL DEFAULT 0
    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_response_cache_used ON ai_response_cache(last_used_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_response_cache_model ON ai_response_cache(provider_id, model)")

    cursor.execute('''CREATE TABLE IF NOT EXISTS lorebook (id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, content TEXT)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS triggers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        label       TEXT    NOT NULL DEFAULT '未命名触发器',
        target_node_id INTEGER NOT NULL,
        mode        TEXT    NOT NULL DEFAULT 'soft',
        cond_type   TEXT    NOT NULL DEFAULT '',
        cond_value  TEXT    NOT NULL DEFAULT '',
        conditions  TEXT    NOT NULL DEFAULT '[]',
        fired       INTEGER NOT NULL DEFAULT 0
    )''')
    try:
        cursor.execute("SELECT conditions FROM triggers LIMIT 1")
    except Exception:
        cursor.execute("ALTER TABLE triggers ADD COLUMN conditions TEXT NOT NULL DEFAULT '[]'")

    cursor.execute('''CREATE TABLE IF NOT EXISTS timelines (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        label           TEXT    NOT NULL DEFAULT '时间线',
        color           TEXT    NOT NULL DEFAULT '#5b9cf5',
        current_node_id INTEGER,
        current_room_id INTEGER,
        memory          TEXT    NOT NULL DEFAULT '',
        char_ids        TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL DEFAULT 'active',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS world_entities (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type  TEXT NOT NULL DEFAULT 'npc',
        name         TEXT NOT NULL UNIQUE,
        location     TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'active',
        last_seen_by TEXT NOT NULL DEFAULT '',
        state_desc   TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        room_id      INTEGER
    )''')

    init_rag_tables()

    cursor.execute('''CREATE TABLE IF NOT EXISTS memory_l1 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_name  TEXT    NOT NULL DEFAULT '',
        player_action TEXT  NOT NULL DEFAULT '',
        ai_summary  TEXT    NOT NULL DEFAULT '',
        thought_process TEXT NOT NULL DEFAULT '',
        entity_updates TEXT NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    try:
        cursor.execute("ALTER TABLE memory_l1 ADD COLUMN timeline_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_effects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id     INTEGER NOT NULL UNIQUE,
        payload     TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
    )''')

    init_map_tables()

    try: cursor.execute("ALTER TABLE characters ADD COLUMN inventory TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE characters ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE characters ADD COLUMN personality TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE characters ADD COLUMN role_brief TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE characters ADD COLUMN script_brief TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE characters ADD COLUMN opening_prompt TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE map_rooms ADD COLUMN floor INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN room_id INTEGER")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE timelines ADD COLUMN current_room_id INTEGER")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE nodes ADD COLUMN expanded_content TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE nodes ADD COLUMN scene_image TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS npc_chat_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        npc_name   TEXT    NOT NULL,
        sender     TEXT    NOT NULL CHECK(sender IN ('player','npc')),
        message    TEXT    NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_npc_chat_npc ON npc_chat_logs(npc_name)")
    try: cursor.execute("ALTER TABLE npc_chat_logs ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS game_checkpoints (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        from_node_id INTEGER NOT NULL,
        snapshot     TEXT    NOT NULL,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    try: cursor.execute("ALTER TABLE game_checkpoints ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError: pass

    cursor.execute('''CREATE TABLE IF NOT EXISTS chronicle_log (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id          INTEGER NOT NULL DEFAULT 0,
        scene_name        TEXT    NOT NULL DEFAULT '',
        scene_content     TEXT    NOT NULL DEFAULT '',
        expanded_content  TEXT    NOT NULL DEFAULT '',
        player_action     TEXT    NOT NULL DEFAULT '',
        created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chronicle_scene ON chronicle_log(scene_id)")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('last_chronicle_position', '0')")

    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('session_memory', '【跑团记忆日志已初始化】\n')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_current_scene_id', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_image', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_prompt', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_ai_text', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_url', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_name', '')")

    conn.commit()
    conn.close()
