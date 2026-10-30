# Cloud 平台（Codex B）

后续栈：Python + FastAPI、PostgreSQL；Vue 3 + Vite + TypeScript + ECharts；Docker Compose。
backend/ 与 frontend/ 仅保留目录。本阶段无业务服务或部署配置。

后续范围：设备认证、幂等事件接收、历史查询、分析任务状态机、浅色 Dashboard、Compose。具体服务器资源待定。

云端应独立运行，使用共享 Schema，无需端侧进程或模型环境。必须遵守根 AGENTS.md 和 docs/；禁止修改 shared/ 和端侧。UI 细节见 docs/ui-requirements.md。
