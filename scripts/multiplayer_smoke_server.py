from __future__ import annotations

import atexit
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.database import configure_database, init_db
from server.auth import auth_router, configure_auth, init_auth_tables
from server.map import init_map_tables
from server.multiplayer import configure_multiplayer, init_multiplayer_tables, multiplayer_router
from server.rag import init_rag_tables


fd, DB_PATH = tempfile.mkstemp(prefix="zric_multiplayer_page_", suffix=".db")
os.close(fd)


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except OSError:
            pass


atexit.register(_cleanup)

configure_database(DB_PATH)
configure_auth(DB_PATH)
configure_multiplayer(DB_PATH)
init_db(init_map_tables=init_map_tables, init_rag_tables=init_rag_tables)
init_auth_tables()
init_multiplayer_tables()

conn = sqlite3.connect(DB_PATH)
try:
    conn.executemany(
        """
        INSERT INTO characters (name, role, hp, san, inventory, personality, status)
        VALUES (?, 'PC', 10, 50, '', '', 'active')
        """,
        [("Alice",), ("Bob",), ("Carol",)],
    )
    conn.commit()
finally:
    conn.close()

app = FastAPI()
app.include_router(auth_router)
app.include_router(multiplayer_router)


@app.get("/")
def root():
    return FileResponse(ROOT / "web" / "multiplayer.html")


@app.get("/multiplayer.html")
def multiplayer_page():
    return FileResponse(ROOT / "web" / "multiplayer.html")


@app.get("/api/game/stat-labels")
def stat_labels():
    return {"status": "success", "labels": {"hp": "HP", "san": "SAN"}}


@app.get("/api/game/worldview")
def worldview():
    return {"status": "success", "worldview": ""}


@app.get("/api/game/memory")
def memory():
    return {"status": "success", "memory": ""}


@app.get("/api/game/lorebook")
def lorebook():
    return {"status": "success", "lorebook": []}


@app.get("/api/world-entities")
def world_entities():
    return {"status": "success", "entities": []}


@app.get("/api/rag/documents")
def rag_documents():
    return {"status": "success", "documents": []}


@app.get("/api/rag/documents/{doc_id}")
def rag_document(doc_id: int):
    return {"status": "success", "document": None, "chunks": []}


@app.get("/api/map/rooms")
def map_rooms(map_id: int = 1):
    return {"status": "success", "rooms": [], "edges": []}


@app.get("/api/timelines")
def timelines():
    return {"status": "success", "timelines": []}
