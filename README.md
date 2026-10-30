# 多模态生理信号端云协同健康监测智能体

架构初始化基线；当前仅包含协议、抽象接口、文档和 Schema 测试，没有业务服务或真实模型集成。

数据流：多模态采集 → CIX P1 专用模型 → Qwen3-4B Agent / 确定性规则 → 本地展示与缓存 → 云端历史分析。

| 目录 | 职责 |
| --- | --- |
| edge-platform/ | 端侧独立应用，Codex A 负责 |
| cloud-platform/ | 云端独立应用，Codex B 负责 |
| shared/ | 冻结的 Python/JSON/OpenAPI 契约 |
| docs/ | 架构、协议、开发、集成与 UI 要求 |
| tests/ | 共享协议测试 |

Windows 快速验证（无需硬件）：

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m shared.export_contracts
```

请从 [架构](docs/architecture.md)、[API 契约](docs/api-contract.md) 和 [并行规则](AGENTS.md) 开始。不存在可启动的完整前后端；不要把导出工具当成业务服务器。
