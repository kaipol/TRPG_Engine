from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.database import configure_database, init_db
from server.auth import auth_router, configure_auth, init_auth_tables
from server.map import init_map_tables, set_db_file as map_set_db_file
from server.multiplayer import (
    configure_multiplayer,
    init_multiplayer_tables,
    multiplayer_router,
)
from server.rag import configure_rag, init_rag_tables


def _seed_game_state(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO nodes (id, name, summary, content, expanded_content, scene_image)
            VALUES (1, 'Start', 'Opening scene', 'You stand in the foyer.', '', '')
            """
        )
        conn.execute(
            """
            INSERT INTO nodes (id, name, summary, content, expanded_content, scene_image)
            VALUES (2, 'Kitchen', 'Second scene', 'The kitchen smells of rain.', '', '')
            """
        )
        option_id = conn.execute(
            "INSERT INTO options (node_id, text, next_node_id) VALUES (1, 'Enter the kitchen', 2)"
        ).lastrowid
        conn.execute(
            """
            INSERT INTO map_rooms (id, map_id, label, x, y, w, h, description, state, color, node_id, floor)
            VALUES (1, 1, 'Foyer', 0, 0, 120, 80, '', 'explored', '#1e3a2f', 1, 1)
            """
        )
        kitchen_room_id = conn.execute(
            """
            INSERT INTO map_rooms (map_id, label, x, y, w, h, description, state, color, node_id, floor)
            VALUES (1, 'Kitchen', 160, 0, 120, 80, '', 'unknown', '#1e3a2f', 2, 1)
            """
        ).lastrowid
        conn.executemany(
            """
            INSERT INTO characters (name, role, hp, san, inventory, personality, status)
            VALUES (?, 'PC', 10, 50, '', '', 'active')
            """,
            [("Alice",), ("Bob",), ("Carol",)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('player_current_scene_id', '1')"
        )
        conn.commit()
        return {"option_id": int(option_id), "kitchen_room_id": int(kitchen_room_id)}
    finally:
        conn.close()


def _assert_status(resp, expected: int, label: str) -> dict:
    if resp.status_code != expected:
        raise AssertionError(f"{label}: expected HTTP {expected}, got {resp.status_code}: {resp.text}")
    try:
        return resp.json()
    except ValueError:
        return {}


def _register_account(client: TestClient, username: str, display_name: str) -> tuple[str, str]:
    data = _assert_status(
        client.post(
            "/api/auth/register",
            json={"username": username, "password": "secret123", "display_name": display_name},
        ),
        200,
        f"register {username}",
    )
    return data["token"], data["account"]["player_id"]


def _checkpoint_count(client: TestClient, code: str) -> int:
    snap = _assert_status(client.get(f"/api/multiplayer/rooms/{code}"), 200, "fetch room snapshot")
    return int(snap.get("checkpoint_count", -1))


def _map_room_state(db_path: str, room_id: int) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT state FROM map_rooms WHERE id=?", (room_id,)).fetchone()
        return row["state"] if row else ""
    finally:
        conn.close()


def _run_save_archive_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="zric_save_contract_", ignore_cleanup_errors=True) as base_dir:
        os.environ["ZRIC_BASE_DIR"] = base_dir
        from server import main as app_main

        client = TestClient(app_main.app)
        save_auth = _assert_status(
            client.post(
                "/api/auth/register",
                json={"username": "save.owner", "password": "secret123", "display_name": "Save Owner"},
            ),
            200,
            "register save owner",
        )
        save_headers = {"X-Auth-Token": save_auth["token"]}
        other_auth = _assert_status(
            client.post(
                "/api/auth/register",
                json={"username": "save.other", "password": "secret123", "display_name": "Other Owner"},
            ),
            200,
            "register other save account",
        )
        other_headers = {"X-Auth-Token": other_auth["token"]}
        conn = app_main.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO nodes (id, name, summary, content, expanded_content, scene_image)
                VALUES (1, 'Archive Start', 'Smoke summary', 'Smoke content', '', '')
                """
            )
            conn.execute(
                "INSERT INTO options (node_id, text, next_node_id) VALUES (1, 'Continue', 1)"
            )
            conn.execute(
                """
                INSERT INTO characters (name, role, hp, san, inventory, personality, status)
                VALUES ('Archivist', 'PC', 10, 50, 'notebook', '', 'active')
                """
            )
            conn.execute(
                "INSERT INTO lorebook (keywords, content) VALUES ('smoke', 'archive verification')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('worldview', 'Smoke worldview')"
            )
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('session_memory', 'Smoke memory')"
            )
            conn.commit()
        finally:
            conn.close()

        exported = _assert_status(
            client.post("/api/game/export", headers=save_headers, json={"save_name": "smoke archive"}),
            200,
            "export campaign save",
        )
        folder = exported.get("folder")
        if not folder or exported.get("path") != f"campaigns/{folder}":
            raise AssertionError(f"export campaign save: unexpected body: {exported}")
        if not exported.get("download_url"):
            raise AssertionError(f"export campaign save: missing download_url: {exported}")

        folder_path = Path(base_dir) / "campaigns" / folder
        if not (folder_path / "campaign.json").is_file() or not (folder_path / "manifest.json").is_file():
            raise AssertionError(f"export campaign save: missing exported files in {folder_path}")

        campaigns = _assert_status(client.get("/api/campaigns", headers=save_headers), 200, "list exported campaigns")
        listed = [item for item in campaigns.get("files", []) if item.get("name") == folder]
        if not listed or not listed[0].get("deletable") or not listed[0].get("download_url"):
            raise AssertionError(f"list exported campaigns: exported save not listed correctly: {campaigns}")
        other_campaigns = _assert_status(
            client.get("/api/campaigns", headers=other_headers),
            200,
            "list other account campaigns",
        )
        if any(item.get("name") == folder for item in other_campaigns.get("files", [])):
            raise AssertionError(f"list other account campaigns: private save leaked: {other_campaigns}")

        download_url = f"/api/game/saves/{quote(folder, safe='')}/download"
        archive = client.get(download_url, headers=save_headers)
        if archive.status_code != 200:
            raise AssertionError(f"download exported save: expected HTTP 200, got {archive.status_code}: {archive.text}")
        with zipfile.ZipFile(BytesIO(archive.content)) as zf:
            names = set(zf.namelist())
        required = {f"{folder}/campaign.json", f"{folder}/manifest.json"}
        if not required.issubset(names):
            raise AssertionError(f"download exported save: ZIP missing {required - names}; got {sorted(names)}")
        other_download = client.get(download_url, headers=other_headers)
        if other_download.status_code != 403:
            raise AssertionError(
                f"download private save as other account: expected HTTP 403, got {other_download.status_code}: {other_download.text}"
            )
        other_load = client.post(
            "/api/game/load",
            headers=other_headers,
            json={"filename": f"campaigns/{folder}"},
        )
        if other_load.status_code != 403:
            raise AssertionError(
                f"load private save as other account: expected HTTP 403, got {other_load.status_code}: {other_load.text}"
            )

        deleted = _assert_status(client.delete(f"/api/game/saves/{quote(folder, safe='')}", headers=save_headers), 200, "delete exported save")
        if deleted.get("name") != folder or folder_path.exists():
            raise AssertionError(f"delete exported save: folder still exists or wrong body: {deleted}")
        missing = client.get(download_url, headers=save_headers)
        if missing.status_code != 404:
            raise AssertionError(f"download deleted save: expected HTTP 404, got {missing.status_code}: {missing.text}")


def main() -> None:
    fd, db_path = tempfile.mkstemp(prefix="zric_multiplayer_contract_", suffix=".db")
    os.close(fd)
    try:
        configure_database(db_path)
        configure_auth(db_path)
        map_set_db_file(db_path)
        configure_rag(db_path)
        configure_multiplayer(db_path)
        init_core = lambda: init_db(init_map_tables=init_map_tables, init_rag_tables=init_rag_tables)
        init_core()
        init_auth_tables()
        init_multiplayer_tables()
        seeded = _seed_game_state(db_path)

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(multiplayer_router)
        client = TestClient(app)

        room = _assert_status(
            client.post("/api/multiplayer/rooms", json={"name": "Contract Room", "settings": {"max_players": 2}}),
            200,
            "create room",
        )
        code = room["room"]["code"]
        if room["room"]["max_players"] != 2:
            raise AssertionError(f"create room: max_players not normalized to 2: {room['room']}")

        p1_auth, p1_id = _register_account(client, "player.one", "Player One")
        p2_auth, p2_id = _register_account(client, "player.two", "Player Two")
        p3_auth, p3_id = _register_account(client, "player.three", "Player Three")
        p1_auth_headers = {"X-Auth-Token": p1_auth}
        p2_auth_headers = {"X-Auth-Token": p2_auth}
        p3_auth_headers = {"X-Auth-Token": p3_auth}

        p1 = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/join",
                headers=p1_auth_headers,
                json={"player_id": p1_id, "display_name": "Player One"},
            ),
            200,
            "join p1",
        )
        p2 = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/join",
                headers=p2_auth_headers,
                json={"player_id": p2_id, "display_name": "Player Two"},
            ),
            200,
            "join p2",
        )
        full = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/join",
                headers=p3_auth_headers,
                json={"player_id": p3_id, "display_name": "Player Three"},
            ),
            409,
            "reject third join",
        )
        if "人数已满" not in str(full.get("detail", "")):
            raise AssertionError(f"reject third join: unexpected error detail: {full}")

        if _checkpoint_count(client, code) != 0:
            raise AssertionError("initial room snapshot should not contain checkpoints")

        action_before_claim = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/action",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"actor_id": p1_id, "actor_name": "Player One", "action": "look around"},
            ),
            403,
            "reject action before role claim",
        )
        if "请先确认扮演角色" not in str(action_before_claim.get("detail", "")):
            raise AssertionError(f"reject action before role claim: unexpected detail: {action_before_claim}")
        if _checkpoint_count(client, code) != 0:
            raise AssertionError("rejected action before role claim must not create a checkpoint")

        claim_multi = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/characters/claim",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"player_id": p1_id, "character_ids": [1, 2]},
            ),
            400,
            "reject multi-character claim by one player",
        )
        if "每位玩家只能选择一个角色" not in str(claim_multi.get("detail", "")):
            raise AssertionError(f"reject multi-character claim: unexpected detail: {claim_multi}")

        p1_claim = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/characters/claim",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"player_id": p1_id, "character_ids": [1]},
            ),
            200,
            "claim p1",
        )
        if p1_claim["room"]["claimed_character_count"] != 1:
            raise AssertionError(f"claim p1: expected one claimed character: {p1_claim['room']}")

        p2_claim = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/characters/claim",
                headers={**p2_auth_headers, "X-Member-Token": p2["member_token"]},
                json={"player_id": p2_id, "character_ids": [2]},
            ),
            200,
            "claim p2",
        )
        if p2_claim["room"]["claimed_character_count"] != 2 or p2_claim["room"]["character_slots_remaining"] != 0:
            raise AssertionError(f"claim p2: room limits wrong after full claim: {p2_claim['room']}")

        conflict = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/characters/claim",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"player_id": p1_id, "character_ids": [3]},
            ),
            409,
            "reject claim change after lock",
        )
        if "不能重新选择" not in str(conflict.get("detail", "")):
            raise AssertionError(f"reject claim change after lock: unexpected detail: {conflict}")

        ok_action = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/action",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"actor_id": p1_id, "actor_name": "Player One", "action": "look around"},
            ),
            200,
            "allow action after role claim",
        )
        if ok_action.get("status") != "success":
            raise AssertionError(f"allow action after role claim: unexpected body: {ok_action}")
        if _checkpoint_count(client, code) != 1:
            raise AssertionError("accepted player action should create exactly one checkpoint")

        advanced = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/action",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={
                    "actor_id": p1_id,
                    "actor_name": "Player One",
                    "action": "enter the kitchen",
                    "action_type": "choice",
                    "option_id": seeded["option_id"],
                    "next_node_id": 2,
                    "option_text": "Enter the kitchen",
                },
            ),
            200,
            "advance scene through player option",
        )
        advanced_state = advanced.get("game_state") or {}
        if advanced_state.get("current_scene_id") != 2:
            raise AssertionError(f"advance scene through player option: scene did not advance: {advanced_state}")
        if advanced_state.get("current_room_id") != seeded["kitchen_room_id"]:
            raise AssertionError(f"advance scene through player option: map room did not advance: {advanced_state}")
        if not advanced.get("scene_message"):
            raise AssertionError(f"advance scene through player option: missing scene message: {advanced}")
        if _map_room_state(db_path, seeded["kitchen_room_id"]) != "explored":
            raise AssertionError("advance scene through player option: target map room was not marked explored")
        if _checkpoint_count(client, code) != 2:
            raise AssertionError("option advance should create a second checkpoint")

        rolled_back = _assert_status(
            client.post(
                f"/api/multiplayer/rooms/{code}/rollback",
                headers={**p1_auth_headers, "X-Member-Token": p1["member_token"]},
                json={"player_id": p1_id},
            ),
            200,
            "rollback option advance",
        )
        rollback_state = rolled_back.get("game_state") or {}
        if rollback_state.get("current_scene_id") != 1:
            raise AssertionError(f"rollback option advance: scene was not restored: {rollback_state}")
        if rollback_state.get("current_room_id") not in (None, ""):
            raise AssertionError(f"rollback option advance: map room was not restored: {rollback_state}")
        if any((msg.get("payload") or {}).get("source") == "scene_advance" for msg in rolled_back.get("messages", [])):
            raise AssertionError(f"rollback option advance: scene advance message survived rollback: {rolled_back['messages']}")
        if _map_room_state(db_path, seeded["kitchen_room_id"]) != "unknown":
            raise AssertionError("rollback option advance: target map room state was not restored")
        if int(rolled_back.get("checkpoint_count", -1)) != 1:
            raise AssertionError(f"rollback option advance: checkpoint count was not decremented: {rolled_back}")

        _run_save_archive_smoke()

        print("multiplayer contract smoke passed")
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
