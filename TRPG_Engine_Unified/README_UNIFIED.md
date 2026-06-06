# TRPG Engine Unified

本目录是 `TRPG_Engine` 与 `ZRIC-AI-TRPG-Engine` 融合后的主应用目录。

## 目录结构

- `main.py`：统一站点的 FastAPI 入口，保留 ZRIC 的 AI-KP、RAG、记忆、地图、手机私信等能力。
- `multiplayer.py`：新增多人联机桥，提供房间、聊天、骰子、剧本上传、地图 Token 同步与 WebSocket 广播。
- `multiplayer.html`：多人联机 VTT 页面，整合房间、地图、聊天、投骰和剧本上传。
- `trpg_engine_plugin/`：原 `TRPG_Engine` 插件源码与资源，包含骰子解析、CoC/DND 数据、角色卡、日志绘制工具等。
- `campaigns/`：ZRIC 示例剧本与知识库。

## 启动方式

```powershell
cd E:\TRPG_Engine\TRPG_Engine_Unified
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

访问：

- `http://127.0.0.1:8000/multiplayer.html`：多人联机入口。
- `http://127.0.0.1:8000/`：ZRIC 原控制台。
- `http://127.0.0.1:8000/phone.html`：模拟手机通讯录/私信界面。

## 运行时数据

统一应用会在本目录下生成 `rpg_game.db`、日志和 `uploads/` 上传资源。这些文件属于本地运行数据，已在仓库根 `.gitignore` 中排除。
