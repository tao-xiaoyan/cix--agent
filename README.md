# 基于 CIX P1 的云边协同个性化睡眠健康监测

项目将 Qwen3-4B 的任务规划与 CIX P1 的专用模型推理解耦。Qwen 在板端 CPU 上选择 Skill，Executor 按注册表校验参数和数据授权，再调用 NOE。验证器将模型输出、输入哈希和执行证据绑定到同一次 ToolResult，工作台和云端据此展示结果。

```mermaid
flowchart LR
  Cam["Orbbec Gemini 335"] --> BMP["实时 BMP"] --> Desk["端侧 Vue Workbench"]
  Desk --> API["板端 FastAPI"]
  API --> Agent["Qwen3-4B CPU<br/>Planner / Registry / Executor / Validator"]
  Cloud["云端 Vue / FastAPI / PostgreSQL"] -->|PPG 任务| API
  Agent --> SLP["静态深度姿态 Skill<br/>授权 SLP 冻结帧"]
  Agent --> PPG["睡眠分期 Skill<br/>授权 AAUWSS epoch"]
  SLP --> NPU["CIX P1 / NOE"]
  PPG --> NPU
  NPU -->|结构化执行证据| API
  API -->|结果回传| Cloud
```

| 演示 | 输入 | 输出 |
| --- | --- | --- |
| `depth_posture_analysis` | 已登记 SLP 静态深度帧，默认样本 `s101_p08` | 仰卧、左侧卧或右侧卧，以及 CIX 执行证据 |
| `sleep_stage_inference` | 已登记 AAUWSS `12/1`，30 秒 PPG 窗口 | 睡眠分期、模型和输入 SHA-256、CIX 执行证据 |
| Orbbec 实时预览 | Gemini 335 连续采集 | 经板端 FastAPI 读取的实时 BMP |

## 最小启动

1. **准备 CIX P1 资产。** 下载固定版 Qwen GGUF 和 CIX 25Q3 helper，按说明提取 AAUWSS epoch。两个 CIX 模型和 SLP 冻结输入待授权交付。[资产来源与 SHA-256](docs/demo-assets.md)
2. **先启动普通 PC 的云端与 SSH 转发，再启动板端。** 云端 Compose 提供 PostgreSQL 16、FastAPI 和 Vue。板端运行 `bash edge-platform/deploy/start-demo.sh`，启动 Qwen 和 FastAPI。只演示静态深度时可省略云端，Orbbec 相机为可选预览。[启动顺序与命令](docs/demo-reproduction.md)
3. **打开浏览器。** 端侧 Vue 的 Workbench 提交 `s101_p08`，云 Vue 提交固定 AAUWSS `12/1`。[演示步骤](docs/demo-reproduction.md#5-浏览器演示)

## 演示与实机证据

在端侧 Workbench 请求已登记的 `s101_p08`。历史板端记录返回 `supine`，worker 打开 `/dev/aipu` 并记录 NOE 同步推理。然后从云 Vue 为登记设备提交 AAUWSS `12/1`。历史实机记录显示板端 NOE 返回 `N1`，PostgreSQL 保存一条结果事件，Vue 回读成功。没有板卡的评委可以直接审查模型与输入哈希、原始输出、任务关联和源记录哈希。[脱敏实机证据](docs/hardware-evidence.md)

## 已知边界

- 实时相机预览与静态 SLP 推理独立运行。实时 Y16 帧尚未接入姿态 Skill，姿态推理按请求使用独立 worker。
- 云睡眠分期使用授权 PPG 样例，当前演示未把新采集的 PPG 映射到分期输入。证据中样例标注是 Wake，模型输出是 N1。记录用于展示执行、对照和追溯能力，不能作为临床准确性或总体性能结论。
- 实机证据来自历史验收版本。复现说明使用同一组已校验模型和执行源码。

工程入口为 `edge-platform/`、`cloud-platform/` 和 `shared/`。后端与共享契约回归保留在各平台及 `tests/`。
