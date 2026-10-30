# API v1 契约

REST JSON 使用 UTF-8，路径统一 /api/v1；/health 与 /ws/realtime 为固定运维/实时入口，WS 消息中的事件仍带 schema_version。静态 OpenAPI 文件位于 shared/。所有时间传入带时区 ISO 8601，响应为 UTC Z。

| 平台 | 方法及路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| Edge | GET /health | 无 | 200 HealthResponse |
| Edge | GET /api/v1/device/status | 无 | 200 DeviceStatus |
| Edge | GET /api/v1/events/recent | EventQuery 查询参数 | 200 Page[Event] |
| Edge | WS /ws/realtime | 认证后服务端推送 | RealtimeMessage |
| Cloud | GET /health | 无 | 200 HealthResponse |
| Cloud | POST /api/v1/events/batch | BatchRequest | 200 BatchResponse |
| Cloud | GET /api/v1/events | EventQuery 查询参数 | 200 Page[Event] |
| Cloud | GET /api/v1/events/{event_id} | UUID 路径 | 200 Event |
| Cloud | POST /api/v1/analysis/tasks | AnalysisRequest + Idempotency-Key | 202 AnalysisTask |
| Cloud | GET /api/v1/analysis/tasks/{task_id} | UUID 路径 | 200 AnalysisTask |
| Cloud | GET /api/v1/devices | cursor、limit | 200 Page[DeviceStatus] |

DTO 精确定义在 shared/schemas/api.py；分页为 `{items: [...], next_cursor: null或字符串}`。limit 默认 50，范围 1～200。事件可选 device_id、event_type、start、end；时间条件为 timestamp >= start 且 timestamp < end，提供两端时 start 必须早于 end。排序 timestamp 降序、event_id 降序，端侧 recent 同样规则。

游标为服务端生成的不透明键集令牌，绑定认证身份、过滤条件及第一页接收时间快照；后续页保持过滤条件，快照之后接收的事件下次新查询可见，避免迟到数据扰动翻页。无下一页时 null。非法或不匹配游标返回 400，过期也返回 400 cursor_expired，客户端重新查询。devices 按 device_id 升序键集分页。设备列表只返回授权范围。

## 批量上传与幂等

请求 `{events: [原始事件对象, ...]}`，1～100 条，总体 JSON 上限建议 1 MiB，最终实现必须统一采用此上限；超限 413。BatchRequest 故意不预先校验每项为 Event，服务端逐条调用 TypeAdapter(Event)，坏项不会阻止有效项持久化。

格式正确且完成逐项处理返回 200，即使全失败也返回完整 results。每个输入索引恰有一个结果：`{index, event_id, status, error}`；无法读取 UUID 时 event_id=null。accepted 表示事务已提交，duplicate 表示之前已确认相同内容；rejected 为永久失败（validation_error、unsupported_schema、event_id_conflict、device_mismatch），retryable 为暂时存储失败（storage_unavailable）。失败项必须带 ErrorDetail；成功项 error=null。不要在提交前确认 accepted。

全局 event_id 唯一；按 Pydantic 归一后的 JSON（含默认值、UTC、排序键）比较内容。相同 UUID 且同内容为 duplicate；同 UUID 不同内容为 rejected/event_id_conflict，不覆盖已有事件。服务端先鉴权再去重，不通过重复响应泄露其他设备信息。令牌设备身份与每条 device_id 不符时该项 rejected，不产生写入。

完整信封格式错误返回 422；JSON 语法错误返回 400；认证失败 401/403 在处理任何项之前返回；容量限制 429、整体不可用 503 可以整批重试。5xx/超时的处理结果可能未知，必须用原 event_id 重试。

端侧生成 UUID 后将事件与 outbox 状态在同一本地事务中落盘；状态 pending → in_flight → acknowledged，进程崩溃后 in_flight 回到 pending。仅 accepted/duplicate 可标记已确认；retryable 保留，rejected 移入本地隔离区并告警。离线不断重新生成 UUID。

重试建议指数退避 1 秒起、上限 60 秒并加入随机抖动，遵守 Retry-After；网络错误、408、429、5xx 和 retryable 可重试；401/403 暂停等待认证恢复；其他 4xx 隔离或人工修正。磁盘容量、原始数据留存及窗口节流由用户确认；默认不得静默删除未确认事件，空间不足显式告警并停止接收可丢弃的高频数据，不能伪造成功。

## 分析任务与结果回写

AnalysisRequest 指定 device_id、start/end（同半开时间区间）、analysis_type=multimodal_summary。必须使用 Idempotency-Key（1～128 字符），身份+键在任务保留期间唯一；同键同归一请求返回原 task_id，同键不同请求 409。状态 queued → running → succeeded/failed，终态不可逆。created_at/updated_at 为服务端 UTC。

成功任务必须先持久化一条 cloud_analysis_result 事件，再填写 result_event_id；失败填写 error。GET 任务轮询获得结果 ID，再 GET 事件详情。端侧校验事件后以相同 UUID 写入本地事件入口并向 WS 推送，标记云来源且不再次上传，避免同步循环。

未来回写预留 `POST /api/v1/cloud-results`（Edge），请求为单条 CloudAnalysisEvent，响应 `{event_id,status: accepted|duplicate}`；与本地事件入口共用幂等逻辑。该接口本阶段不实现、不纳入当前可用 OpenAPI。后续必须单独确定云服务身份认证及网络可达性；默认采用端侧主动轮询，无需开放公网入站端口。

## 错误及认证

统一错误：`{request_id:"...",error:{code:"validation_error",message:"...",details:{}}}`。400 参数/游标错误；401 缺失或无效认证；403 越权；404 不存在或不可见；409 幂等键冲突；413 过大；422 结构或字段不合法；429 限流；503 不可用。所有响应附 X-Request-ID，服务端生成或校验传入值。实现应转换 FastAPI 默认校验错误为此结构，不返回栈或密钥。

最小认证采用每设备独立随机 Bearer token，经 TLS 传输，服务端保存哈希及 device_id、权限、撤销状态，用户离线注册配置。设备可上传/查询自己的事件及任务，不能任意列出其他设备。Dashboard 使用独立 operator token 及设备授权范围；不能复用任意设备令牌。令牌从环境/密钥配置注入，禁止放进 Git、URL 或日志。/health 免认证且不泄露敏感指标；其余接口都鉴权，端侧默认只绑定回环地址。

浏览器原生 WebSocket 无法设置 Authorization，使用同源 HttpOnly、SameSite=Strict 会话 Cookie；由后续 Edge 的 `POST /api/v1/session` 用 Bearer token 换短期会话，响应 `{expires_at: UTC时间}` 并 Set-Cookie（HTTPS 下 Secure）。该支持接口由 A 实现时补充其平台 OpenAPI，不改变共享事件契约。WS 握手验证 Cookie 和 Origin；非浏览器可用 Bearer，禁止 query token。WS 建连后每个连接 sequence 从 0 单调递增，推送 event；每 15 秒 heartbeat，无消息 45 秒客户端重连。队列满时发送 gap 并以 1013 关闭；鉴权失效以 1008 关闭。重连后先查 recent 补齐、按 event_id 去重；sequence 只检测本连接丢帧，不能替代持久化游标，WS 不保证持久回放。
