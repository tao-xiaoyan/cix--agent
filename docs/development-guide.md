# 本地开发与并行流程

Windows 从仓库根运行 README 中命令。Python 3.11+、Pydantic 2 为协议基线；Node.js LTS 及前端依赖由各任务初始化时固定版本。requirements-dev.txt 仅为协议开发依赖，不是 AI 模型依赖。

本阶段无真实端口监听、数据库实例、前端或 Docker Compose。后续端侧后端建议 8001、前端 5173；云端后端 8002、前端 5174，数据库仅容器内/本地访问，配置均从环境读取。CORS 使用明确本地来源白名单。

当前目录未初始化 Git；用户审阅基线并完成首次提交后，可执行：

```powershell
git worktree add -b codex/edge ../health-monitor-edge
git worktree add -b codex/cloud ../health-monitor-cloud
```

此处仅提供命令，本任务不在工作区外创建目录。把 A 指向 edge worktree，B 指向 cloud worktree；分别授予各平台目录的修改范围。合并前检查 diff 不越界，两个任务都不得改 shared/。契约变更通过提案交架构任务处理，更新基线后各分支同步。

平台内部需配置自己的依赖文件和测试；不要把开发 venv、个人数据、模型权重提交。两平台构建时只打包自身代码与固定版本 shared/。ARM64 依赖及镜像必须在实际部署前验证，不能把 Windows 环境直接复制到设备。

最小质量门：共享 unittest 全通过、生成契约无非预期变化、Mock 符合八类事件结构、接口和认证符合文档；平台业务检查由后续任务补充。本初始化不执行部署、SSH、Git 推送或真实模型操作。
