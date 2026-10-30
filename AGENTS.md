# 并行开发边界

本仓库采用稳定共享契约。执行任何开发前阅读 docs/ 下的文档。

## Codex A
- 只能修改 edge-platform/，负责端侧 FastAPI、WebSocket、实时 Dashboard 和 Mock Adapter。
- 不得修改 cloud-platform/、shared/、根文件、docs/ 或 tests/。

## Codex B
- 只能修改 cloud-platform/，负责云端 FastAPI、PostgreSQL、历史 Dashboard、任务 API 和 Docker Compose。
- 不得修改 edge-platform/、shared/、根文件、docs/ 或 tests/。

## 共享契约
- shared/ 为冻结的 1.0.0 协议；A、B 不得自行修改或复制后分叉。
- 变更需求先在自己平台目录内写 contract-change-proposal.md，说明原因、兼容性、迁移及测试方案，由架构任务统一更新。
- 各平台内部测试放在自己的目录。接口实现必须通过共享 Pydantic 模型验证。
- 推荐独立分支和 Git worktree；不在同一工作目录同时开发。架构提交完成后，两分支从相同基线创建。
- 不接入真实硬件、不修改模型运行环境、不下载模型、不提交密钥、不自动推送。

本初始化架构任务允许创建根文件、shared/、docs/、tests/ 和两平台骨架；此权限不转授给 A、B。
