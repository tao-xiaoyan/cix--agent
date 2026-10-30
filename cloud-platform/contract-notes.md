# 云端 MVP 契约补充与限制

共享事件和已定义 REST DTO 不做修改，所有事件输入/输出使用 shared.schemas。

## 云端只读扩展
- GET /api/v1/dashboard：operator 认证；查询采用 EventQuery 的 device_id、event_type、start、end。返回 event_count、anomaly_count、device_count、latest_sync、event_types（类型→数量）、devices（device_id、event_count、last_sync）。前四项事件统计遵循当前筛选；device_count 与 devices 为全部授权注册设备、设备累计统计，UI 有独立标签。
- GET /api/v1/analysis/tasks：受设备范围约束的 Page[AnalysisTask]，支持 device_id、limit、cursor。按 task_id 升序，绑定身份、设备过滤和创建时间快照，游标有效期一小时。
- 既有 endpoints 的字段与 status code 保持不变。设备身份上传限定自身；operator 可以在明确的设备授权范围内操作。配置不支持通配设备范围。

## 已有协议空缺，不自行扩展事件
- agent_decision 只有 actions 字符串与 execution_status，没有调度目标、结构化工具调用/结果、call_id；UI 展示“协议未提供”，不会从文本推断为真实执行。
- 任务 DTO 没有原请求区间或单条异常绑定；UI 将设备任务列表与事件关联列表分别显示，不能声称某个设备任务正在分析某条异常。
- DeviceStatus 无 last_sync 和 event_count；只读 dashboard 扩展提供这些管理统计。last_seen 当前表示最近一次成功上传/去重请求时间；90 秒内视为 online，仅代表同步活动，不证明硬件连接。
- 生产设备注册、token 发放、撤销和轮换由离线环境配置完成。示例仅提供随机本地凭证生成脚本，不创建公开注册接口。
- Mock 结果 source=mock，model_name=mock-analysis-worker，事件类型仍为 cloud_analysis_result。端侧必须按该类型识别回写，不以 source=cloud 作为唯一条件。

## MVP 运行边界
单 API 进程、单内置 Mock Worker；请不要用 uvicorn --workers >1。queued 保存在数据库，重启继续处理；running 在重启时标记 failed，避免假装任务已完成。成功时结果事件、analysis_results 和任务 succeeded 在一个数据库事务中提交。没有真实模型、诊断、第三方 AI 调用或入站回调。

游标签名且绑定身份、授权设备集合、过滤条件与接收时间快照。事件按 timestamp/event_id 降序。并发长事务若在快照前设置 received_at、但在快照后提交，仍存在边界可见性限制；当前事务按单事件提交且无长事务。严格跨请求 MVCC 快照是后续扩展点。

Dashboard 精确总数来自数据库聚合；曲线窗口均值、睡姿分布和最近异常只基于已加载分页记录，页面显式说明范围，可继续加载。结构化睡眠质量、医疗阈值、数据编辑/删除、审计、生产限流与大规模查询优化尚未实现。
