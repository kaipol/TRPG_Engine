# Z.R.I.C TRPG Engine

AI 驱动的 TRPG 主持与多人跑团工具。当前仓库已经收束为单一主目录：`E:\TRPG_Engine` 就是应用根目录；所有代码按功能归类到同一个项目结构下，不再保留独立插件壳或独立子项目。

## 启动主程序

在 PowerShell 中运行：

```powershell
pip install -r requirements.txt
python main.py
```

启动成功后会自动打开 GM 控制台，也可以手动访问：

- GM 控制台：`http://127.0.0.1:8000/`
- 投屏端：`http://127.0.0.1:8000/player.html`
- 手机通讯录：`http://127.0.0.1:8000/phone.html`
- 多人联机桌：`http://127.0.0.1:8000/multiplayer.html`

首次使用 AI 功能时，在前端填写 API Key，或复制 `.env.example` 为 `.env` 后填入：

- `OPENAI_COMPAT_API_KEY`
- `OPENAI_COMPAT_BASE_URL`
- `OPENAI_COMPAT_CHAT_MODEL`
- `OPENAI_COMPAT_EMBEDDING_MODEL`
- `OPENAI_COMPAT_IMAGE_MODEL`
- `OPENAI_COMPAT_STT_MODEL`
- `OPENAI_COMPAT_TTS_MODEL`

前端 API 供应商配置面板支持保存多个 OpenAI 兼容供应商 profile，并写入本地 `openai_providers.json`，便于在不同端点之间切换。

不要提交 `.env`、数据库、日志或上传文件。

## 统一目录结构

```text
E:\TRPG_Engine
├── main.py                         # 根目录兼容启动入口
├── server/                         # FastAPI 后端与核心推演模块
│   ├── main.py                     # app 入口：server.main:app
│   ├── agent.py                    # AI 推演
│   ├── rag.py                      # RAG 知识库
│   ├── memory.py                   # 记忆系统
│   ├── map.py                      # 空间地图
│   ├── trigger.py                  # 触发器
│   ├── entity.py                   # 世界实体/NPC
│   ├── timeline.py                 # 多时间线
│   ├── multiplayer.py              # 多人联机 API/WebSocket
│   ├── dice.py                     # TRPG_Engine 原生骰子/规则 API
│   └── trpgdice/                   # 内置骰子、CoC、先攻、法术资料服务
├── web/                            # GM、投屏、手机、多人桌页面
├── campaigns/                      # 剧本与知识库
├── docs/                           # 补充集成说明
└── uploads/                        # 运行时上传资源，忽略提交
```

## 功能概览

- AI 推演：OpenAI 兼容端点模型列表获取、搜索、选择与请求级回退，并支持多供应商切换。
- RAG 知识库：剧本文档切片、统一端点 embedding、语义检索。
- 图像 / STT / TTS：均通过同一个 OpenAI 兼容端点配置各自模型。
- 记忆系统：短期上下文、实体记忆、长期知识注入。
- 地图与触发器：场景拓扑、物品/状态/AI 条件触发。
- 多端展示：GM 控制台、玩家投屏、手机私信、多房间联机桌。
- 骰子与规则服务：`/api/dice/*` 原生后端 API，支持通用掷骰、CoC 检定/SAN、先攻、随机表、角色生成与法术查询。

## 发布前检查

应提交的核心静态资源：

- `campaigns/`
- `server/trpgdice/component/config.default.yaml`
- `server/trpgdice/data/mania.json`
- `server/trpgdice/data/phobias.json`
- `server/trpgdice/data/sealdice-builtins/`

不应提交的本地运行数据：

- `.env`
- `.runtime/`
- `.venv/`
- `uploads/`
- `*.db`
- `*.log`
- `node_modules/`
- `dist/`

## 许可证

根目录 `LICENSE` 为原 TRPGdice 代码许可证；`LICENSE.txt` 为 Z.R.I.C 引擎许可证。保留两份许可证是为了标记融合前代码来源，当前骰子能力已作为 TRPG_Engine 原生服务运行。
