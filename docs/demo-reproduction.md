# 最小运行路径

从仓库根目录开始。真实 Skill 需要 Orange Pi 6 Plus 上的 CIX P1 与 NOE，实时预览另需 Gemini 335 和 ARM64 Orbbec SDK。普通 PC 可运行浏览器、Vue 和云端 Compose。Qwen 规划在板端 CPU 上运行。[固定资产和驱动版本](demo-assets.md)

## 1. 板端相机

按 Orbbec 官方步骤安装 SDK 和 USB udev 规则，设置 `ORBBEC_SDK_ROOT` 指向 SDK 解包目录。使用 C++17 编译器和 CMake 构建并安装用户服务。已有验证过的相机服务可继续使用，其 BMP 路径通过 `EDGE_DEPTH_PREVIEW_PATH` 指定。

```bash
camera_src=edge-platform/deploy/orbbec
camera_home="$HOME/.local/share/cix-demo/orbbec"
cmake -S "$camera_src" -B "$camera_src/build" -DOrbbecSDK_DIR="$ORBBEC_SDK_ROOT/lib" -DCMAKE_BUILD_TYPE=Release
cmake --build "$camera_src/build"
install -d -m 700 "$camera_home/live"
install -m 755 "$camera_src/build/depth_live" "$camera_home/depth_live"
install -m 644 "$camera_src/depth_watchdog.py" "$camera_home/depth_watchdog.py"
install -d "$HOME/.config/systemd/user"
install -m 644 "$camera_src"/systemd/* "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now depth-camera.service depth-watchdog.timer
```

FastAPI 读取 `$camera_home/live/depth.bmp`。相机同时发布 Y16 文件。watchdog 每 5 秒检查 BMP，超过 12 秒未更新或文件缺失时请求恢复采集。

## 2. 板端 Qwen、FastAPI 和端侧前端

在板端配置资产路径。深度资产目录包含仓库中与冻结校验值匹配的 worker、预处理代码和清单，以及授权取得的输入文件和 `.cix`。厂商 helper 根目录下应有 `utils/NOE_Engine.py`，其哈希由启动器核对。若同时演示云端 PPG，先准备下一节中的四个云端变量，再启动板端 API。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r edge-platform/backend/requirements.txt
export CIX_DEMO_STATE_DIR="$HOME/.local/state/cix-demo"
export CIX_QWEN_MODEL=/path/to/verified/Qwen3-4B-Q4_K_M.gguf
export EDGE_DEPTH_ASSETS=/path/to/verified/depth
export EDGE_DEPTH_NPU_PYTHON=/path/to/noe-venv/bin/python
export CIX_NOE_SITE_PACKAGES=/path/to/noe-venv/lib/python3.11/site-packages
export CIX_NOE_HELPER_ROOT=/path/to/vendor/vision
bash edge-platform/deploy/start-demo.sh
```

脚本启动回环 `18080` 的 llama-server 和回环 `18551` 的 FastAPI。需要时用 `CIX_EDGE_PORT` 调整板端 API 端口。操作员令牌由私有环境提供，或由脚本写入状态目录下权限为 600 的 `operator.token`。使用该令牌登录端侧 Vue。若明确复用已有 llama-server，先独立核对运行中的模型文件哈希，再设 `CIX_REUSE_QWEN=1`。

普通 PC 开启 SSH 转发，然后在另一终端启动 Vue。

```bash
ssh -N -L 8001:127.0.0.1:18551 orangepi@BOARD_HOST
```

```bash
cd edge-platform/frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`，检查实时预览，在 Workbench 提交已登记的 `s101_p08` 静态任务并查看 CIX 执行证据。

## 3. 云端 PPG

在运行云端 Compose 的普通 PC 上，用 Python 创建本地演示凭据，再启动云服务。脚本拒绝覆盖已有 `.env`。已有部署可直接使用已登记的设备和操作者。Compose 后端启动时运行 Alembic。

```bash
python cloud-platform/backend/scripts/init_dev.py
cd cloud-platform
docker compose up --build -d
```

云 Vue 位于 `http://127.0.0.1:5174`，本机云 API 位于 `127.0.0.1:8002`。默认回环配置可以从云主机另开终端建立反向 SSH 转发，使板端能用 `http://127.0.0.1:18002` 领单。使用可直达的非回环云部署时，配置 HTTPS 地址。

```bash
ssh -N -R 127.0.0.1:18002:127.0.0.1:8002 orangepi@BOARD_HOST
```

在板端启动器的环境中提供 `EDGE_CLOUD_URL`、`EDGE_CLOUD_TOKEN`、`EDGE_DEVICE_ID`、`EDGE_SLEEP_STAGE_ASSETS`。默认本地初始化生成设备 ID `mock-device-001`，对应设备凭据在云主机的本地 `.env` 中。通过私有方式配置该凭据，在云 Vue 使用独立的操作者令牌。提交 `aauwss/12/1` 后检查任务完成、CIX 证据和结果回读。[历史实机摘录](hardware-evidence.md)
