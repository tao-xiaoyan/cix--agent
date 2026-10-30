# Cloud 历史分析与管理平台 MVP

独立 FastAPI + PostgreSQL + Vue 3/Vite/TypeScript/ECharts 应用。所有业务文件在 cloud-platform/，复用仓库根 shared/；无需端侧设备、模型或 NPU。Mock 结果明确标记，不用于医学判断。

## Windows 开发准备

从 **D:\health-monitor-cloud** 打开 PowerShell。建议 Python 3.11+、Node.js 20 LTS、Docker Desktop（Linux containers）。不要复制其他 worktree 的虚拟环境。

```powershell
python -m venv cloud-platform/backend/.venv
cloud-platform/backend/.venv/Scripts/python.exe -m pip install -r cloud-platform/backend/requirements.txt
cloud-platform/backend/.venv/Scripts/python.exe cloud-platform/backend/scripts/init_dev.py
```

生成 cloud-platform/.env（随机数据库密码、游标签名密钥、身份 token 哈希）和 .operator-token.local（仅本地 UI 登录 token）。脚本不覆盖已有配置；二者都被 Git 忽略，不要分享或提交。设备明文 token 只在本地 .env 中供导入脚本使用，服务端身份表只读取哈希。撤销配置项可加 revoked=true，轮换需要更新配置后重启。

## 启动数据库与迁移

```powershell
cd cloud-platform
docker compose up -d db
cd backend
$env:PYTHONPATH='D:\health-monitor-cloud;D:\health-monitor-cloud\cloud-platform\backend'
.venv/Scripts/python.exe -m alembic upgrade head
.venv/Scripts/python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002 --workers 1 --no-access-log
```

数据库仅绑定 127.0.0.1:5433，数据保存于 Compose volume。迁移代码位于 backend/migrations/，查看版本 `.venv/Scripts/python.exe -m alembic current`；生成变更需评审后使用 revision --autogenerate，不在应用启动时 create_all。

另一个终端启动前端：

```powershell
cd D:\health-monitor-cloud\cloud-platform\frontend
npm ci
npm run dev
```

访问 http://127.0.0.1:5174；从 cloud-platform/.operator-token.local 读取 token 粘贴到登录框。token 只放 Vue 内存，断开或刷新即清除，不写 localStorage。页面由真实 API 查询，未连接不展示假统计。API 地址 http://127.0.0.1:8002；交互文档 /docs；健康检查 /health。

## 导入 Mock 与分析闭环

从 worktree 根执行：

```powershell
cloud-platform/backend/.venv/Scripts/python.exe cloud-platform/backend/scripts/import_mock.py
```

脚本经设备 Bearer token 上传 168 条合成事件，覆盖过去 24 小时，每次新 UUID；未连接硬件。mock-channel 的 synthetic 单位仅是合成演示标签，不暗示真实传感器协议。刷新 UI：选择设备/日期可查看历史，异常或 Agent 页面选择设备及起止时间后创建 Mock 分析；页面每两秒轮询未结束任务。端侧可 POST /api/v1/analysis/tasks（必须带 Idempotency-Key），GET 任务取得 result_event_id，再 GET /api/v1/events/{id} 获取共享结果事件。回写本地时不要再次上传该结果。

## Docker Compose 全栈

从 cloud-platform/ 执行 `docker compose up --build -d`。API 镜像先运行 Alembic，再启动单进程 Worker；前端 Nginx 将 /api 代理到 API。构建上下文为 worktree 根，以包含只读 shared/；Dockerfile 专用 ignore 排除 .env、venv、node_modules、Git 元数据。所有映射仅绑定回环地址，不部署公网。

## 测试

从 worktree 根执行：

```powershell
$env:PYTHONPATH='D:\health-monitor-cloud;D:\health-monitor-cloud\cloud-platform\backend'
$env:PYTHONDONTWRITEBYTECODE='1'
cloud-platform/backend/.venv/Scripts/python.exe -m pytest cloud-platform/backend/tests -q -p no:cacheprovider
cloud-platform/backend/.venv/Scripts/python.exe -m unittest discover -s tests -v
cd cloud-platform/frontend
npm run build
```

默认每个测试独立 SQLite 数据库，用于快速 API/存储验证；不能替代 PostgreSQL 集成测试。PostgreSQL 测试在唯一随机 schema 内建表/清理，不碰应用数据：

```powershell
cd D:\health-monitor-cloud\cloud-platform
$env:TEST_DB_PASSWORD=[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
docker compose -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL="postgresql+psycopg://test:$env:TEST_DB_PASSWORD@127.0.0.1:5544/health_monitor_test"
cd ..
cloud-platform/backend/.venv/Scripts/python.exe -m pytest cloud-platform/backend/tests -q -p no:cacheprovider
cd cloud-platform
docker compose -f compose.test.yaml down
```

测试库使用 tmpfs。应用数据库迁移另以 `alembic upgrade head` 和 `alembic check` 验证；不要对已有生产数据执行 downgrade。前端类型由冻结 Schema 生成，更新架构基线后在 frontend/ 执行 `npm run generate` 并审查差异；不手写事件结构。

## 部署准备与尚未完成

生产前需配置域名/TLS 反向代理、精确 CORS、用户级凭证发放与撤销、数据库备份和恢复、请求限流、数据库容量/留存和监控。反向代理需保护 Authorization，不记录认证信息；当前 Uvicorn 禁用访问日志以避免查询细节泄露。MVP 不直接暴露公网，不含真实 AI、诊断、告警规则、完整工具执行审计、角色管理 UI、数据删除或生产级任务队列。

五个管理页面包括总览、历史、异常、Agent 和设备；字段缺口及只读扩展见 contract-notes.md。实际测试和环境限制见 TEST-RESULTS.md。

## 无 Docker 的本地 UI 验收

第二轮已保留 8002 后端和 5174 前端运行，使用独立 SQLite 验收库。新浏览器访问 http://127.0.0.1:5174，将 .operator-token.local 内容粘贴到认证框并连接。

如需重启后端，先确保 8002 空闲，从 worktree 根执行：

```powershell
$env:PYTHONPATH='D:\health-monitor-cloud;D:\health-monitor-cloud\cloud-platform\backend'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='sqlite:///D:/health-monitor-cloud/cloud-platform/.test-data/acceptance.sqlite'
cloud-platform/backend/.venv/Scripts/python.exe -m alembic -c cloud-platform/backend/alembic.ini upgrade head
cloud-platform/backend/.venv/Scripts/python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8002 --no-access-log
```

另一个终端在 frontend/ 运行 npm run dev。SQLite 用于本地验收，不替代 PostgreSQL 测试。
