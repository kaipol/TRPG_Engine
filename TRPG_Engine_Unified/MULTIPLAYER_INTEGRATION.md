# Z.R.I.C + TRPG_Engine 多人融合说明

本目录现在提供一个一体化多人跑团入口：

- `http://127.0.0.1:8000/multiplayer.html`

## 已融合能力

- 房间：创建/加入多人房间，维护成员在线状态。
- 公屏：房间级聊天消息持久化并通过 WebSocket 实时同步。
- 骰子：复用 `TRPG_Engine/component/roll/dice.py` 的表达式解析与 CoC 判定能力，支持 `/r 1d20+5`、`/r 1d100` 等。
- AI-KP 骰点反馈：骰子结果会写入房间事件流，并生成 AI 可读叙事反馈；无 API Key 时自动降级为确定性文本。
- 剧本导入：房间设置中上传 TXT、Markdown、PDF，写入 ZRIC RAG 表，后续推演可检索。
- VTT 地图：支持上传背景图、创建/拖拽 Token，坐标通过房间 WebSocket 多端同步。
- ZRIC 原生能力保留：`index.html`、`player.html`、`phone.html`、RAG、记忆、NPC、地图拓扑、触发器、时间线等原模块继续可用。

## 新增后端模块

- `multiplayer.py`
  - REST:
    - `GET /api/multiplayer/health`
    - `GET/POST /api/multiplayer/rooms`
    - `GET/PATCH /api/multiplayer/rooms/{room_code}`
    - `POST /api/multiplayer/rooms/{room_code}/join`
    - `GET/POST /api/multiplayer/rooms/{room_code}/messages`
    - `POST /api/multiplayer/rooms/{room_code}/dice`
    - `GET/POST /api/multiplayer/rooms/{room_code}/tokens`
    - `PUT /api/multiplayer/rooms/{room_code}/tokens/{token_id}/move`
    - `POST /api/multiplayer/rooms/{room_code}/scenario/upload`
    - `POST /api/multiplayer/rooms/{room_code}/map/background`
  - WebSocket:
    - `WS /ws/rooms/{room_code}`

## 数据表

- `multiplayer_rooms`
- `multiplayer_members`
- `multiplayer_messages`
- `multiplayer_tokens`
- `multiplayer_room_documents`

这些表与 ZRIC 原有 SQLite 数据库共存。当前实现优先保证单进程本地/局域网多人可用；未来如果要部署公网高并发，建议迁移到 PostgreSQL，并用 Redis Pub/Sub 替换进程内 WebSocket 广播集合。

## 启动

```bash
pip install -r requirements.txt
python main.py
```

然后访问：

- GM 控制台：`http://127.0.0.1:8000/`
- 多人跑团桌：`http://127.0.0.1:8000/multiplayer.html`
- 投屏端：`http://127.0.0.1:8000/player.html`
- NPC 通讯录：`http://127.0.0.1:8000/phone.html`

## 注意

- `DEEPSEEK_API_KEY` 未配置时，多人骰点仍可用，AI-KP 反馈会降级为规则化文本。
- PDF 剧本上传依赖 `pypdf`，表单上传依赖 `python-multipart`，均已写入 `requirements.txt`。
- 当前房间 RAG 文档会写入全局 `rag_documents/rag_chunks`，并用 source 前缀标记房间代码；后续可以进一步把 ZRIC 推演检索限定到当前房间文档。
