# CIX P1 实机执行证据

以下字段摘自已有板端与云端归档，模型 SHA-256 与 [资产清单](../edge-platform/deploy/assets.json) 对照。脱敏摘录保留执行与关联所需字段，原始记录留在维护者归档。没有板卡时可审查执行路径和版本，历史记录的 SHA-256 可用于核验日后取得的原件。

## 静态深度 Workbench

V0.4 注册样本 `s101_p08`。Agent 状态为 `finished`，板端 worker 记录 `CIX NOE NPU`、`success` 和已打开的 `/dev/aipu`。一次同步 NOE 调用为 **0.148486 ms**，原始 INT8 为 `[92, -128, -128]`，输出 `supine`。同一案例记录 Skill 耗时 **354.46 ms**、Agent 耗时 **56.37 s**。三项分别为同步 API、工具与 Agent 端到端时间。

- 模型 SHA-256 `ad33423c78e738a9f5f14c1a4e4195725f41c4eab7bd76323fde6a73a10a7478`
- 冻结个体帧输入 SHA-256 `9f13916b4e232dbaab123350b467fd33653d5384795964e74849b781eda7c6bb`
- worker 执行记录标识 `89e0ccd4f38a470788846c8a84e256bb`。原件 `worker.json` SHA-256 `2b3b2d22a2d4692c349bd93088ab2713e8912e9707a9da6671b04f18133e6f69`
- Agent 验收汇总原件 `verified-summary.json` SHA-256 `1c53d92f785701dd749ea93a61c0bb5cc2ff9885b876482a8f4a628151afbb9d`

## 云端 PPG 任务

2026-11-22，PostgreSQL 隔离验收提交了已登记 `aauwss/12/1`。云端存档状态为 `succeeded`，记录一条结果事件并确认 Vue 浏览器结果可见。ToolResult 证据标记 `CIX NOE NPU`，同步推理记录 **0.946703 ms**，输出 `N1`。样本标注为 Wake。本次结果证明了真实执行和跨端回读。

- 任务 ID `fcce8c45-4460-4cd4-9c8e-88b9babb81c8`，结果事件 ID `8a1f3da7-beaf-4b15-a40b-fb49a5a4c383`
- Agent run ID `4750818a-f2fb-43dd-a08a-d8b7a422565c`，ToolCall ID `2351757f-03da-4265-9fbd-8aa759786294`
- 模型 SHA-256 `f24597fe2ec04170e0684e9c4266b7b05802dde3901833ed5264456c8ef92854`，授权 NPY 文件 SHA-256 `3e01b8c3005fa3b413850beb0d1a94231e25e9df89eec81d76309de79909ce92`
- 云结果原件 `verified-result.json` SHA-256 `d01400b22d9c6af4ab0e90f3d8cc65981fd2d9a01eee8d7661f7b63a64e7c0e2`；同次 PostgreSQL/Vue 验收汇总原件 `summary.json` SHA-256 `b8d4880332d7e37b90a87148dd57d9711322687133252b8922d51b9bf9158a8b`

## 计时口径

另一次板端分阶段追踪中，同一 PPG 样例完成三次 NOE 调用，分别耗时 **0.224503、0.213424、0.258473 ms**。包含两轮 Qwen 和证据校验的 Agent 端到端为 **62.777、51.436、51.332 s**。运行汇总原件 `summary.json` SHA-256 为 `d27f3282912271a102000249ad61172656512980a38e7fbab61a735fae502975`。结果适用于这三次串行执行。推荐保留独立进程的深度实现，历史对照曾观察到常驻 graph 在更换输入后复用前次输出。
