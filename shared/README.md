# 稳定协议 1.0.0

从仓库根运行 Python；使用 `from shared.schemas import Event` 和 `TypeAdapter(Event)` 校验。接口位于 `shared.interfaces`，仅使用标准库和 Pydantic。

- schemas/events.py：八种事件及判别联合。
- schemas/api.py：REST/WS 请求响应 DTO。
- schemas/event.schema.json：生成的 JSON Schema，供 TypeScript 和跨语言校验使用。
- examples/events.json：八类事件示例，不含真实个人数据。
- openapi-edge.json / openapi-cloud.json：静态 REST 契约；WS 见文档。
- export_contracts.py：离线导出工具；依赖 FastAPI，不启动监听、不访问网络。

以 Python 模型为结构真源，docs/api-contract.md 为行为真源；变更后同步导出、示例和测试。额外字段被拒绝；新增字段也须协调版本与消费者。A/B 不得修改本目录。

部署时各平台镜像独立复制 shared/，保持 `/app/shared` 与平台程序同级并设置 PYTHONPATH=/app；无需另一平台进程。后续可由架构任务将它打包成固定版本内部 wheel。
