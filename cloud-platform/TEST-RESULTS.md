# 实际验证记录

验证环境：Windows，Python 3.13.5，Node 18.0.0。测试只写 cloud-platform/ 及隔离临时目录。

| 检查 | 实际结果 |
| --- | --- |
| 云端 API/数据库测试 | 9 passed；每测试独立 SQLite |
| 共享 Schema 测试 | 8 passed；未修改共享文件 |
| Alembic upgrade/downgrade | SQLite 往返通过 |
| 真实 HTTP 闭环 | 通过：启动 FastAPI → Alembic 建库 → API 导入 168 Mock 事件 → 历史查询 → 创建任务 → 内置 Worker 完成 → 查询结果 |
| 前端事件类型生成 | 直接读取 shared/schemas/event.schema.json，成功 |
| vue-tsc 与 Vite build | 通过；存在单个 JS 包大于 500kB 的优化提示 |
| Vite dev 实际启动 | HTTP 200，验证后关闭 |
| PostgreSQL 集成测试 | 未运行，本机无 PostgreSQL / Docker 可执行程序 |
| Docker Compose 启动/配置命令验证 | 未运行，本机无 Docker；提供 compose.yaml 与 compose.test.yaml |
| 浏览器交互/视觉回归 | 未执行；不能由构建和 HTTP 200 替代 |

后端测试包含认证失败/越权、部分成功、去重/冲突、UTC 归一、JSON/体积/字段校验、时间边界、游标分页及快照、任务幂等与结果持久化、Worker 中断恢复、设备/统计读取、迁移往返。

复现 HTTP 冒烟：从 worktree 根运行 `cloud-platform/backend/.venv/Scripts/python.exe cloud-platform/backend/scripts/smoke.py`。使用独立 SQLite 文件与 18002 端口；自动关闭所启动进程。此结果不等同于 PostgreSQL 上验证通过。

依赖警告：Starlette TestClient 使用的 AnyIO 别名存在弃用提示；当前测试通过。前端 Schema 生成工具的间接依赖有弃用提示。未进行生产安全审计或负载测试。

完整 PostgreSQL 验收仍待完成：按 README 启动隔离 PostgreSQL、执行迁移和 TEST_DATABASE_URL 测试，再运行完整 Compose。尚不能将本次结果标记为全部十项验收均通过。

## 第二轮实际运行验收（2026-10-30）

- FastAPI 保持运行：127.0.0.1:8002；Vue Vite 保持运行：127.0.0.1:5174。
- 使用现有 Windows Python 虚拟环境，DATABASE_URL 仅在服务启动进程覆盖为 sqlite:///D:/health-monitor-cloud/cloud-platform/.test-data/acceptance.sqlite；.env 中的 PostgreSQL 目标配置不改变。
- Alembic 迁移成功，经设备 API 导入 168 条 Mock 记录。API 和浏览器各创建一个 Mock 任务，均 succeeded，最终 170 条事件、24 条异常、1 台设备。
- health、events、单事件详情、devices、dashboard、tasks 列表、创建及查询任务与读取结果均通过真实 HTTP；无认证返回 401；允许来源 CORS 预检成功。
- 浏览器五页切换成功：总览统计和图表、历史记录、异常列表、Agent 决策、设备统计均来自真实 API。音频类型筛选返回 24 条；2030 年日期范围返回 0 条并显示空状态；选择 mock-device-001 生效。
- 页面创建任务观察到 queued → succeeded，点击 MOCK 结果打开详情。所有模拟记录有 MOCK 标识。
- 正常操作期间浏览器 error/warn 日志为空；故意使用无效 token 验证 401 错误提示，随后已恢复有效认证。
- 1280px 浏览器检查未发现页面级横向溢出（文档宽 1265px）；截图已在会话展示，截图写入本地受到浏览器工具权限限制，未保存成文件。
- 加载状态存在于实现中；本机响应很快，未通过网络节流对加载状态作可视化时序验收。移动端、不同缩放比例与键盘无障碍仍需人工检查。
- Docker 与 psql 命令均不可用，本轮未安装系统软件，PostgreSQL 运行验证仍待具备相应环境。
- 服务日志在 .test-data/api.log、api-error.log、ui.log、ui-error.log。所有验收数据和凭证被 Git 忽略。
