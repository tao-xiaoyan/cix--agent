# 云端任务与历史页面

Docker Compose 运行 PostgreSQL 16、FastAPI 和 Vue/Nginx。API 容器先应用 Alembic 迁移，再提供任务提交、设备领单、完成回传和结果查询。云端 PPG 任务由板端 NOE 执行，云端保留证据关联。容器端口默认绑定本机回环。

[云端启动、身份与板端连通](../docs/demo-reproduction.md#3-云端-ppg) · [实机闭环记录](../docs/hardware-evidence.md)
