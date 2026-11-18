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

## 本地睡眠分析 Agent

`edge-platform/backend/sleep_analysis/` 提供端侧后端可直接调用的本地接口。
从主仓库根运行时，将 `edge-platform` 加入 Python 导入路径，然后使用下列接口接入规划器。

```python
from pathlib import Path

from backend.sleep_analysis.agent import create_agent
from backend.sleep_analysis.contracts import WindowRequest

async def analyze(planner, recording_dir, session_id, start, end, activities=()):
    agent = create_agent(planner, datasets={"offline": Path(recording_dir)}, activities=activities)
    request = WindowRequest(
        dataset_id="offline",
        session_id=session_id,
        window={"start": start, "end": end},
    )
    return await agent.run(request)
```

规划器实现异步 `plan(request, skills, trace)`，接收本次分析范围、Skill 的输入和输出 JSON Schema、已有调用轨迹。
每次返回共享 `ToolCall` 或本地 `Finish`。`ScriptedTestPlanner` 只存在于测试中。调用须在登记的工具、数据集、
会话和分析时间范围内。返回轨迹包含调用 ID、参数、结果和起止 UTC 时间。单次规划和工具调用有超时，
每次运行有调用次数上限，同一运行内的重复调用 ID 会终止运行。普通工具失败会进入下一轮规划的上下文。
Data Inspection 在线程中执行有读取上限的 SQLite 查询。工具超时会终止编排，已开始的只读工作会自行结束。

工具封装沿用共享 `ToolResult`。参数错误或越权返回 `denied`，执行异常或超时返回 `failed`。
已正常完成的调用返回 `succeeded`，其中 `data.status` 再区分 `ok`、`partial` 和 `unavailable`。
各工具使用带时区的半开窗口 `[start,end)`，结果回传数据集、会话、时间窗口和证据引用。

| Skill | 输入 | 阶段二返回 |
| --- | --- | --- |
| `data_inspection` | 数据集、会话、恰好 10 秒的窗口 | PPG、GSR/IMU 可用性、样本数、时间估计、拒判原因及证据引用。缺库、未登记数据集或缺依赖有明确原因。 |
| `sleep_activity_analysis` | 数据集、会话、相机活动查询窗口 | 返回窗口内已登记的 depth 或 thermal 事件。无记录时返回 `unavailable/not_implemented`，当前没有相机检测器。 |
| `evidence_verification` | 数据集、会话、包含活动事件的 10 秒核验窗口、已登记事件 ID、可选来源引用和前后对照窗 | 逐窗口的质量、来源和生理/运动摘要，并单独返回对时证据和核验状态。 |
| `historical_comparison` | 数据集、会话、目标与参考窗口、指标版本 | 保留契约。无已接入的真实可比数据，返回 `unavailable/no_comparable_history`。 |

Data Inspection 和 Evidence Verification 延迟加载可信本地 `edge_ppg` 的 `inference.adapter.Adapter`，运行环境需已有
NumPy/SciPy。将该仓库根加入 Python 导入路径，并将预先登记的目录指向离线 SQLite 数据。核验仅读取目标及可选的
前后各一个 10 秒窗口。PPG 使用已保存、通过采集质量门控的滚动脉率，输出可用观测数和中位数。IMU 沿用 GSR/IMU
联合分支质量判定，输出重采样轴的中心化 RMS，单位为原始计数。覆盖率是估算时间跨度与请求窗口的重叠比例，
连续性和拒判原因另行返回。

`RegisteredActivity` 由调用方预先登记，来源引用中的数据集及会话绑定到本地离线数据集。事件需标记
`provenance=detected` 或 `test`，每个活动指标保留单位和定义，算法版本随事件记录。设备指标保留各自口径。
跨设备对时状态由调用方提供，`verified` 需要对时依据 ID 和误差界。同为 UTC 或生理适配器的拟合残差
不足以证明相机与生理记录已对齐。本阶段核验的假设是相机活动是否伴随可观察的佩戴设备运动。
缺少经验证的运动判据时返回 `insufficient_evidence`。因此 `data.status=ok` 表示获得了两类摘要，
核验状态仍单独判定。

### Qwen 规划适配

如果实际端点确认兼容 Chat Completions，使用 `ChatCompletionsClient(http, endpoint, model, token=...)` 构造客户端，
其中 `http` 是调用方管理的 `httpx.AsyncClient`。将 `QwenPlanner(client, task)` 作为上述 `analyze` 的规划器。
其他模型接口可实现异步 `complete(system=..., prompt=...) -> str`，同样使用严格 JSON 内容协议。
请求发送用户任务、授权分析范围、工具参数 Schema 和此前结构化调用轨迹。回复选择单次工具调用或结束时引用的
调用 ID。执行器校验调用范围和参数，程序从所引用的工具结果渲染答复。原始波形、IMU 数组和相机帧留在本地。
输入和输出字符上限可在 `QwenPlanner` 配置。规划和工具超时及调用次数可在 `SleepAnalysisAgent` 配置。
本地测试覆盖模拟模型响应和 HTTP 协议，真实模型端点需独立验证。

在主仓库根使用现有 Python 环境运行测试。设置 `PYTHONPATH` 可覆盖实际 `edge_ppg` 只读适配器集成用例。

```powershell
$env:PYTHONPATH = (Resolve-Path '..\云边端协同\edge_ppg').Path + ';' + (Resolve-Path '.\edge-platform').Path
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider edge-platform/backend/test_sleep_analysis.py
```
