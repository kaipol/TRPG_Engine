# Z.R.I.C TRPG Engine

AI 驱动的 TRPG 主持、剧情推演、知识库检索和多人跑团工具。当前仓库已经收束为单一项目根目录：`E:\TRPG_Engine` 即应用根目录，后端、前端页面、剧本与规则资料都在同一个仓库中维护。

## 快速启动

Windows 下推荐直接双击根目录的 `start.bat`。脚本会自动创建 `.venv`、安装依赖并启动服务。

也可以在 PowerShell 中手动运行：

```powershell
pip install -r requirements.txt
python main.py
```

启动后程序会自动打开 GM 控制台，也可以手动访问：

- GM 控制台：`http://127.0.0.1:8000/`
- 投屏端：`http://127.0.0.1:8000/player.html`
- 手机通讯录：`http://127.0.0.1:8000/phone.html`
- 多人联机桌：`http://127.0.0.1:8000/multiplayer.html`

## AI 配置

首次使用 AI 功能时，可以在前端配置面板填写 OpenAI 兼容端点，也可以复制 `.env.example` 为 `.env` 后填写：

```powershell
Copy-Item .env.example .env
```

常用配置项：

- `OPENAI_COMPAT_API_KEY`
- `OPENAI_COMPAT_BASE_URL`
- `OPENAI_COMPAT_CHAT_MODEL`
- `OPENAI_COMPAT_EMBEDDING_MODEL`
- `OPENAI_COMPAT_IMAGE_MODEL`
- `OPENAI_COMPAT_STT_MODEL`
- `OPENAI_COMPAT_TTS_MODEL`

前端支持保存多个 OpenAI 兼容供应商 profile，并写入本地 `openai_providers.json`。该文件包含本地 API Key，已在 `.gitignore` 中忽略。

模型配置区提供统一的“获取全部模型”按钮。获取成功后，Chat、Embedding、Image、STT 和 TTS 每个模型输入框都会出现自己的下拉框；下拉框顶部带搜索框，可以在已获取模型中筛选并点击填入对应模型 ID。

如果兼容端点的 `/models` 请求失败，后端会保留当前已配置的模型作为兜底选项，避免前端列表为空。这种情况下下拉框可能只显示当前 active model，并会在接口响应的 `error` 字段中返回远端错误原因。

## 目录结构

```text
E:\TRPG_Engine
├── main.py               # 根目录启动入口，运行 server.main:app
├── server/               # FastAPI 后端与核心推演模块
│   ├── main.py           # 应用入口、静态页面、剧本导入、AI 配置 API
│   ├── agent.py          # AI 推演与流式扩写
│   ├── ai_provider.py    # OpenAI 兼容供应商与模型配置
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
├── campaigns/            # 剧本、知识库和地图数据
├── docs/                 # 补充说明
└── uploads/              # 运行时上传资源，不提交
```

## 功能概览

- AI 推演：OpenAI 兼容 chat 模型、模型列表获取、搜索、选择与多供应商切换。
- 剧本导入：支持 PDF、DOCX、TXT、Markdown 导入，并尽量提取图片资源生成可玩剧本。
- RAG 知识库：文档切片、embedding、关键词加向量混合检索。
- 图像 / STT / TTS：统一使用 OpenAI 兼容供应商配置。
- 记忆系统：短期工作区、长期记忆折叠、世界实体状态注入。
- 地图与触发器：场景拓扑、房间状态、物品/AI 条件触发。
- 多端展示：GM 控制台、投屏端、手机私信、多房间联机桌。
- 骰子与规则服务：`/api/dice/*` 支持通用掷骰、CoC 检定/SAN、先攻、角色生成与法术查询。

## 多人联机安全

多人房间创建后会生成 GM 房间令牌，加入房间后会生成成员令牌。前端会把令牌保存在浏览器 `localStorage` 中，并在聊天、骰子、棋子、地图和剧本上传请求中自动携带。

- 房间列表和房间快照可查看，便于玩家加入。
- 聊天、骰子、棋子移动需要成员令牌或 GM 令牌。
- 房间设置、地图背景上传、房间剧本导入需要 GM 令牌。
- 令牌只适合本地或受信任局域网跑团；如果部署到公网，应额外加反向代理认证或访问控制。

## 发布前检查

建议至少运行：

```powershell
python -m py_compile main.py (Get-ChildItem server -Recurse -Filter *.py).FullName
```

不要提交本地运行数据或凭据：

- `.env`
- `openai_providers.json`
- `.runtime/`
- `.venv/`
- `uploads/`
- `*.db`, `*.db-wal`, `*.db-shm`
- `*.log`
- `node_modules/`, `dist/`

## 许可证

根目录 `LICENSE` 为原 TRPGdice 代码许可证；`LICENSE.txt` 为 Z.R.I.C 引擎许可证。保留两份许可证用于标记融合前代码来源，当前骰子能力已作为 TRPG Engine 原生服务运行。
