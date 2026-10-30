# 事件协议 1.0.0

所有事件使用 JSON；具体机器可读结构见 shared/schemas/event.schema.json，Python 真源为 shared/schemas/events.py。

公共字段：schema_version 固定 1.0.0；event_id 为生产者一次生成并持久化的 UUID；device_id 为注册设备标识；timestamp 为事件产生时间；event_type 为八种判别值；source 区分 mock/model/sensor/rule/agent/cloud/system；可选 model_name、model_version 和 [0,1] 的 confidence；payload 按 event_type 严格校验。

| event_type | payload 内容 |
| --- | --- |
| physiological_window | window_start/end、connection、protocol、channels |
| pose_estimation | 坐标系、可选帧尺寸、persons/keypoints、检测状态 |
| sleep_posture | 分类 label、状态、原因；类别集合待用户模型确定 |
| audio_event | 分类 label、持续毫秒、状态、原因 |
| anomaly_event | code、severity、描述及关联事件 |
| agent_decision | 决策、理由、动作、关联事件、执行状态 |
| cloud_analysis_result | task_id、摘要、发现及关联事件 |
| device_metrics | CPU 百分比、内存/磁盘字节、摄氏温度、待上传数量、传感器连接状态 |

时间必须带时区；导出统一 UTC `Z`，例如 `2026-10-30T00:00:00Z`；无时区时间拒绝。边界接受带偏移 ISO 8601 并归一 UTC。窗口允许零长度但结束不得早于开始。设备时钟偏差通过后续运维处理，不能用接收时间覆盖原事件时间。

生理通道 name 来自实际协议元数据；unit 与 sampling_rate_hz 未知时为 null，不默认任何医学单位、采样率或通道。samples 中无效/缺失点用 null，禁止 NaN/Infinity；quality 标明 valid/missing/invalid/unknown，missing_reason 解释原因。混合窗口的 quality 表示整体质量，逐点 null 保留缺口。缺失元数据不得被补猜。connection=disconnected 独立于数据质量，断线时可发空 channels 或缺失窗口，勿补零模拟正常值。

空 persons 表示没有检测到人，unavailable 表示输入或适配器不可用，两者不可混淆。分类不可用时 label 应为 null；有效分类时由生产者填 label。confidence 未提供则为 null，不以 0 代表未知。位置数值按声明坐标系解释；模型适配器负责归一化坐标边界、关键点集合和帧尺寸一致性校验。

Mock 与真实模型完全使用同一入口；source=mock 的数据可展示但必须标识，历史分析默认由业务层明确选择是否纳入。事件不可变；更正应生成新 UUID 并用 related_event_ids 关联（支持该字段的事件）。schema_version 不支持时逐条拒绝，不静默丢字段。协议变更由架构任务统一升级并设计迁移。
