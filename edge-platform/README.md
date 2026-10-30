# Edge 平台（Codex A）

MVP 使用 Python + FastAPI、WebSocket、SQLite；Vue 3 + Vite + TypeScript + ECharts。仅依赖本地 Mock 即可运行。

目标为 Orange Pi 6 Plus / CIX P1，ARM64 Linux，32GB RAM / 256GB SSD；Windows 先用 Mock 开发。

## Windows 启动

在 `D:\health-monitor-edge` 仓库根执行：

```powershell
python -m venv edge-platform\.venv
.\edge-platform\.venv\Scripts\python.exe -m pip install -r edge-platform\backend\requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\edge-platform\.venv\Scripts\python.exe -m uvicorn app:app --app-dir edge-platform\backend --host 127.0.0.1 --port 8001
```

另开终端：

```powershell
cd edge-platform\frontend
npm ci
npm run dev
```

页面 `http://127.0.0.1:5173`；健康检查 `http://127.0.0.1:8001/health`；端侧 REST `http://127.0.0.1:8001/api/v1/`；WebSocket `ws://127.0.0.1:5173/ws/realtime`（经 Vite 代理）。演示令牌为 `local-demo-token`，仅供本地回环开发。生产或局域网访问前请在环境设置随机 `EDGE_OPERATOR_TOKEN`、HTTPS 和受信任的前端来源。配置项见 `.env.example`，应用读取环境变量，不自动读取该文件。

需要 Node.js 20.19+ 或 22+。测试：在仓库根运行 `.\edge-platform\.venv\Scripts\python.exe -m pytest edge-platform/backend/test_app.py -q`；前端在 `edge-platform/frontend` 运行 `npm run build`。

Mock 每 2 秒生成生理、姿态、睡姿、音频和设备事件，每 5 轮生成一次模拟异常，规则调度记录 Agent 决策并将分析请求排入本地任务队列。SQLite 将事件和 outbox 原子写入。配置 `EDGE_CLOUD_URL` 与 `EDGE_CLOUD_TOKEN` 后后台上传批次；云端离线时保留待同步事件。未配置云端时完全本地运行。

`backend/app.py` 的 `AdapterRegistry.register/infer` 是真实模型接入点。分别注册 physiological、vision、audio、llm 的实现，输入/输出遵守 `shared.interfaces.ModelInput/ModelOutput`，事件先经共享 `Event` 校验再进入 `Runtime.record`。关闭 Mock 时设置 `EDGE_MOCK=0`。本 MVP 不加载 YOLO、Qwen、串口、摄像头或 NPU；真实模型的窗口、单位、通道、标签及性能映射须按设备协议确定。

必须遵守根 AGENTS.md 和 docs/ 中所有协议；禁止修改 shared/ 和云端。UI 细节见 docs/ui-requirements.md。
