# 端云 MVP 集成验收（2026-10-31，Asia/Shanghai）

## Git 与运行范围

- 架构基线：6ce2411a51513adba149a782028e5522ca4fa272。
- A 原成果提交：66bab88；B 原成果提交：aec3851。
- 集成分支：integration/edge-cloud-mvp；合并提交 a261965、b29aeaf，无冲突。
- 集成工作区：C:/Users/txy/.codex/worktrees/3628/health-monitor。
- main 不合并、不推送；shared/ 与基线一致。
- 本次测试使用独立 SQLite、随机凭据和本任务子进程；不接入硬件或模型。

## 兼容性与修复

HTTP 路径、Bearer、设备范围、Event/BatchResponse/AnalysisTask 均复用共享 DTO，真实服务可互通。补充端侧 GET /api/v1/analysis/tasks/local（相同操作认证），仅为本地展示扩展，不修改 shared/。

补齐任务轮询、结果读取与关联校验、持久化和 WS 推送；Mock 云结果 source=mock，按 event_type 阻止二次上传。重放创建请求已成功时仍先读取结果再确认本地成功。

真实测试发现任务可能先于新异常上传完成，增加 outbox 确认屏障。云 Worker 关联上限 100 时优先保留以幂等键标识的触发异常，再选择最近事件。依赖增加 websockets，修复普通 uvicorn 安装不能升级 WS 的问题。

## 验证

- 共享 Schema：8/8。
- 端侧：7/7（含新增 Mock 云结果不重新上传回归测试）。
- 云端：9/9（含 Worker 修改后的重跑）。
- 两平台 vue-tsc --noEmit 通过，生产构建通过；均有 chunk 体积提示。
- 真实 TCP/HTTP：异常→规则决策→上传→云持久化→任务→Mock Worker→轮询→事件读取→本地结果可查。
- 重复上传返回 duplicate；同幂等键重复创建返回相同 task_id；云结果不进入端侧 outbox。
- 故障测试：真实 TCP 服务不响应触发 HTTP timeout；停止本任务云服务模拟断网，端侧缓存增长；重启使用原 SQLite，事件保留、积压恢复。
- 任务失败：在隔离测试库将任务置为运行中模拟处理中断，Worker 重启转 failed，端侧读取 worker_interrupted。此为明确故障注入，不代表真实模型故障验证。
- 前端代理真实认证、查询、任务及结果 API 通过，设备数为 1，事件统计与返回数据一致。
- 浏览器操作已验证登录、端侧实时数据/规则决策/任务及结果、云端历史时间线与 Mock 分析结果。最终浏览器确认端侧“实时连接”、持续事件更新；云端 Agent 列出成功/失败任务，点击可打开 Mock 结果详情。真实认证 WebSocket 经 Vite 代理收到共享事件。

## 复现与本地访问

在集成工作区使用独立 venv 安装两平台 requirements，再执行：

```powershell
.\.venv\Scripts\python.exe integration/http_acceptance.py
```

端口必须空闲；脚本不强制结束占用进程。脚本自行迁移新 SQLite，运行后保留服务供人工检查。状态文件 .venv/latest-integration.txt 指向本轮忽略目录；其 session.json 保存本地 operator_token 和自有 PID，不进入 Git。

- Edge API：http://127.0.0.1:8001；UI：http://127.0.0.1:5173。
- Cloud API：http://127.0.0.1:8002；UI：http://127.0.0.1:5174。
- 两页面输入 session.json 的 operator_token。Edge 看实时状态、决策、任务 ID 与结果；Cloud 点击 Agent 调度记录、任务结果、事件详情，对照相同 task_id/event_id/related_event_ids。
- 若需重跑，先核对当前 PID 的命令行和端口，确认属于本轮集成工作区后再停止，不凭旧 PID 文件直接杀进程。

## 限制与后续

Docker/PostgreSQL 命令未检测到，本轮未验证，不安装。ARM64、真实串口、NPU、摄像头、音频和真实模型均未验证。

上传确认屏障依赖队列可排空；高吞吐持续积压时应改成按任务依赖事件确认，不应贸然移除屏障。HTTP timeout 验证与断网重试验证分别执行，尚未覆盖所有“服务端提交后响应丢失”的故障时序。当前单进程 Mock Worker 不是生产任务队列。

端侧既有认证默认演示 token、会话安全属性和任务错误分级仍需生产加固；禁止直接暴露公网。前端依赖复用原 worktree 的 node_modules junction，仅为本机验收，交付部署应 npm ci 独立安装。

真实模型接入应只替换适配器，先确认通道/单位/采样率与质量映射，再验证窗口、推理资源隔离和 ARM64 性能；保持统一事件入口。Mock 结果不可视为医疗分析。

最终联合脚本（含真实 WS）退出码 0；正常、HTTP 超时、断网缓存、云端重启恢复、幂等和任务中断失败传播全部通过。
