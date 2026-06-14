# Z.R.I.C TRPG Engine

AI 驱动的 TRPG 主持、剧情推演、知识库检索和多人跑团工具。当前仓库已经收束为单一项目根目录：`E:\TRPG_Engine` 即应用根目录，后端、前端页面、剧本与规则资料都在同一个仓库中维护。

## 快速启动

Windows 下推荐直接双击根目录的 `start.bat`。脚本会自动创建 `.venv`、安装依赖并启动服务，默认使用端口 `8000`；关闭启动窗口或按 `Ctrl+C` 会停止本次启动的服务。

Linux 下在项目根目录运行一键启动命令：

```bash
bash ./start.sh
```

如果想把脚本设为可执行文件后再启动：

```bash
chmod +x start.sh && ./start.sh
```


启动脚本默认端口是 `8000`。需要修改启动端口时，可以在启动命令后追加端口号：

```powershell
start.bat 8010
python main.py --port 8010
```

Linux/macOS：

```bash
bash ./start.sh 8010
```

Linux 服务器部署时默认监听 `0.0.0.0`，浏览器请访问 `http://<服务器IP>:<端口>/`。如果需要临时指定监听地址，可以传第二个参数：

```bash
bash ./start.sh 8010 0.0.0.0
```

如果首页控制台报 `/assets/index.css` 或 `/assets/index-app.js` 404，通常是上传服务器时漏掉了项目根目录 `assets/`。当前版本会在启动时检查这些文件，缺失会直接报出 `ASSETS_DIR` 路径和缺失文件名。

在 1Panel “网站 → 运行环境”这类部署方式中，运行目录应设置为项目根目录（如 `/home/TRPG_Engine`），并确保项目根目录下存在 `assets/`。所有前端 CSS/JS 统一从 `/assets/...` 访问，不再使用 `web/assets/` 或启动时复制兼容文件。

剧本导入默认会尝试调用已配置的 Chat 模型把原始文档转换为可玩剧本。服务器无法访问模型端点时会在超时后自动生成保底剧本；如需跳过 AI 转换，可在 `config.json` 中设置 `campaign_import.use_ai_conversion=false`。PDF 本地解析运行在独立子进程内，默认 45 秒无结果会终止并继续导入，避免页面长期停在“抽取主剧本文本”。扫描版 PDF 没有文本层时，会在安装 `PyMuPDF` 且 Chat 模型支持图片输入的情况下尝试多模态读取前几页。

可调整的 PDF 导入项：

- `campaign_import.pdf_timeout_seconds`：本地 PDF 文本/图片提取超时，默认 `45`。
- `campaign_import.pdf_max_pages`：最多提取页数，默认 `80`。
- `campaign_import.pdf_image_max_pages`：最多扫描内嵌图片页数，默认 `12`。
- `campaign_import.pdf_max_images`：最多提取内嵌图片数量，默认 `40`。
- `campaign_import.pdf_multimodal_pages`：本地无文本时多模态读取页数，默认 `4`；设为 `0` 可关闭。

Windows 本地启动后程序会自动打开主入口；Linux 服务器启动不会尝试打开浏览器，请从客户端访问服务器地址。主入口提供“单人游玩”和“多人游玩”：两者都由 AI-GM 主持，玩家选择剧中角色进行扮演；GM 控制台保留为高级管理视图。

也可以手动访问：

- 主入口 / 单人玩家桌 / GM 控制台：`http://<服务器IP>:<端口>/`
- 投屏端：`http://<服务器IP>:<端口>/player.html`
- 手机通讯录：`http://<服务器IP>:<端口>/phone.html`
- 多人联机桌：`http://<服务器IP>:<端口>/multiplayer.html`

## AI 配置

首次使用 AI 功能时，在前端配置面板填写 OpenAI 兼容端点。前端支持保存多个 OpenAI 兼容供应商 profile，并写入本地 `config.json`。该文件包含本地 API Key、启动端口、网页端配置项和服务端运行项，已在 `.gitignore` 中忽略。

`config.json` 是唯一的本地运行配置源。项目不再读取 `.env` 中的 `OPENAI_COMPAT_*` 配置，避免同一密钥和模型在两处漂移。首次启动或首次保存配置时会写出包含默认值和 `//` 注释的 `config.json`；旧版 `openai_providers.json` 如存在会被读取并迁移到新文件。

远程部署时，更新 API Key、切换供应商、获取草稿模型、修改 Token 策略和 AI 缓存都受管理员保护。这个保护在 `server/config_api.py` 中实现：如果未配置 `config.json` 的 `security.admin_token`，只有本机回环地址（`127.0.0.1` / `localhost` / `::1`）可以写入 API Key。1Panel、Docker 或服务器公网访问通常不是回环地址，因此需要在 `config.json` 中设置 `security.admin_token`，重启服务后，在前端“OpenAI 兼容供应商”配置面板的“管理员令牌”输入框填写同一段令牌。前端会通过 `X-Admin-Token` 请求头发送该令牌，不会把管理员令牌保存到浏览器。

`config.json` 中新增的运行项只覆盖网页端不能直接配置的服务端设置，例如 `security.admin_token`、`campaign_import`、`rag`、`multiplayer` 限制和 `server.allowed_origins`。网页端已有入口的供应商、模型、Token 策略和 AI 缓存仍由前端配置面板保存，不需要手动改这些字段。

模型配置区提供统一的“获取全部模型”按钮。获取成功后，Chat、Embedding 和 Image 每个模型输入框都会出现自己的下拉框；下拉框顶部带搜索框，可以在已获取模型中筛选并点击填入对应模型 ID。

如果兼容端点的 `/models` 请求失败，后端会保留当前已配置的模型作为兜底选项，避免前端列表为空。这种情况下下拉框可能只显示当前 active model，并会在接口响应的 `error` 字段中返回远端错误原因。

## 目录结构

```text
E:\TRPG_Engine
├── main.py               # 根目录启动入口，运行 server.main:app
├── assets/               # 统一前端 CSS/JS 静态资源，对外路径 /assets/*
├── server/               # FastAPI 后端与核心推演模块
│   ├── main.py           # 应用入口、静态页面、剧本导入、AI 配置 API
│   ├── agent.py          # AI 推演与流式扩写
│   ├── ai_provider.py    # OpenAI 兼容供应商与模型配置
│   ├── local_config.py   # 本地 config.json 读取、默认值、注释模板与旧配置迁移
│   ├── rag.py            # RAG 知识库、切片、embedding、检索
│   ├── memory.py         # 短期/长期记忆
│   ├── map.py            # 场景地图
│   ├── trigger.py        # 触发器
│   ├── entity.py         # 世界实体/NPC 状态
│   ├── timeline.py       # 多时间线
│   ├── multiplayer.py    # 多人房间、聊天、棋子、地图上传
│   ├── dice.py           # 原生骰子/规则 API
│   └── trpgdice/         # 内置 CoC、DND 资料和骰子组件
├── web/                  # GM、投屏、手机、多人桌 HTML 页面
└── campaigns/            # 剧本、知识库和地图数据；多人地图上传生成 campaigns/<剧本名>/mp_map_*
```

## 功能概览

- AI 推演：OpenAI 兼容 chat 模型、模型列表获取、搜索、选择与多供应商切换。
- 剧本导入：支持 PDF、DOCX、TXT、Markdown 导入，并尽量提取图片资源生成可玩剧本。
- RAG 知识库：文档切片、embedding、关键词加向量混合检索。
- 图像生成：统一使用 OpenAI 兼容供应商配置。
- 记忆系统：短期工作区、长期记忆折叠、世界实体状态注入。
- 地图与触发器：场景拓扑、房间状态、物品/AI 条件触发。
- 多端展示：单人玩家桌、GM 控制台、投屏端、手机私信、多房间联机桌。
- 骰子与规则服务：`/api/dice/*` 支持通用掷骰、CoC 检定/SAN、先攻、角色生成与法术查询。

## 多人联机实现

多人跑团入口为 `http://<服务器IP>:<端口>/multiplayer.html`，默认端口 `8000`。

已融合能力：

- 房间：创建/加入多人房间，维护成员在线状态。
- 公屏：房间级聊天消息持久化，并通过 WebSocket 实时同步。
- 骰子：复用 `server.trpgdice.component.roll.dice` 与原生 `/api/dice/*` 服务，支持 `/r 1d20+5`、`/r 1d100` 等。
- AI-KP 骰点反馈：骰子结果会写入房间事件流，并生成 AI 可读叙事反馈；无 API Key 时自动降级为确定性文本。
- 剧本导入：房间设置中上传 TXT、Markdown、PDF，写入 ZRIC RAG 表，后续推演可检索。
- VTT 地图：支持上传背景图、创建/拖拽 Token，坐标通过房间 WebSocket 多端同步。
- ZRIC 原生能力保留：`index.html`、`player.html`、`phone.html`、RAG、记忆、NPC、地图拓扑、触发器、时间线等原模块继续可用。

`server/multiplayer.py` 提供的主要接口：

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
- `WS /ws/rooms/{room_code}`

`server/dice.py` 提供的主要接口：

- `POST /api/dice/roll`
- `POST /api/dice/coc/check`
- `POST /api/dice/coc/versus`
- `POST /api/dice/coc/san`
- `GET /api/dice/coc/insanity`
- `POST /api/dice/coc/rule`
- `GET /api/dice/tool/d66`
- `GET /api/dice/tool/jrrp`
- `GET /api/dice/tool/fireball`
- `POST /api/dice/tool/choose`
- `GET /api/dice/tool/name`
- `GET /api/dice/tool/character/coc`
- `GET /api/dice/tool/character/dnd`
- `GET /api/dice/spell`
- `POST /api/dice/initiative/add`
- `POST /api/dice/initiative/next`
- `POST /api/dice/initiative/clear`

多人联机新增数据表：

- `multiplayer_rooms`
- `multiplayer_members`
- `multiplayer_messages`
- `multiplayer_tokens`
- `multiplayer_room_documents`

这些表与 ZRIC 原有 SQLite 数据库共存。当前实现优先保证单进程本地/局域网多人可用；如果部署到公网高并发环境，建议迁移到 PostgreSQL，并用 Redis Pub/Sub 替换进程内 WebSocket 广播集合。

## 多人联机安全

多人房间创建后会生成 GM 房间令牌，加入房间后会生成成员令牌。前端会把令牌保存在浏览器 `localStorage` 中，并在聊天、骰子、棋子、地图和剧本上传请求中自动携带。

- 房间列表和房间快照可查看，便于玩家加入。
- 聊天、骰子、棋子移动需要成员令牌或 GM 令牌。
- 房间设置、地图背景上传、房间剧本导入需要 GM 令牌。
- 令牌只适合本地或受信任局域网跑团；如果部署到公网，应额外加反向代理认证或访问控制。
- OpenAI 兼容端点未配置时，多人骰点仍可用，AI-KP 反馈会降级为规则化文本。
- PDF 剧本上传依赖 `pypdf`，扫描版 PDF 多模态读取依赖 `PyMuPDF`，表单上传依赖 `python-multipart`，均已写入 `requirements.txt`。
- 当前房间 RAG 文档会写入全局 `rag_documents` / `rag_chunks`，并用 source 前缀标记房间代码；后续如需更强隔离，可进一步把 ZRIC 推演检索限定到当前房间文档。

## 发布前检查

建议至少运行：

```powershell
python -m py_compile main.py (Get-ChildItem server -Recurse -Filter *.py).FullName
```

不要提交本地运行数据或凭据：

- `config.json`
- `openai_providers.json`（旧版本地配置，存在时会迁移）
- `.runtime/`
- `.venv/`
- `campaigns/*/mp_map_*`
- `*.db`, `*.db-wal`, `*.db-shm`
- `*.log`
- `node_modules/`, `dist/`

## 许可证

根目录 `LICENSE` 为原 TRPGdice 代码许可证；`LICENSE.txt` 为 Z.R.I.C 引擎许可证。保留两份许可证用于标记融合前代码来源，当前骰子能力已作为 TRPG Engine 原生服务运行。
