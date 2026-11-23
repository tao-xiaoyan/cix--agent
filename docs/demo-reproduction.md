# 最小实机复现

在普通 PC 和 CIX P1 板端各准备同一发布版本的仓库。从仓库根运行以下命令，云端和 SSH 转发先就绪，再启动板端 Agent，最后打开浏览器。两条固定样例推理只需要 CIX P1，Orbbec 相机用于可选的实时预览。

## 1. 前置环境

- **板端**使用 [已验收的 CIX 驱动、NOE 和 llama-server 版本](demo-assets.md#板卡依赖)。准备普通 API venv 和厂商 NOE venv，确认运行用户有权读写 `/dev/aipu`。
- **普通 PC**安装 Docker Compose、Node.js 22 和 OpenSSH 客户端。示例使用 Bash，其中端云转发要求 SSH 服务允许相应端口转发。

## 2. 资产目录

以下以板端 `$HOME/cix-demo-assets` 为例。两个 `.cix` 与 SLP 冻结数组待授权交付。GGUF、NOE helper 和 AAUWSS 提取方式见 [资产清单](demo-assets.md)。

```text
cix-demo-assets/
  qwen/Qwen3-4B-Q4_K_M.gguf
  depth/
    depth_posture_v03.cix
    run_npu.py
    frozen/
      raw.npy
      inputs.npy
      reference.json
      preprocess_v02.py
  ppg/
    sleep_cnn.cix
    epoch_source.json
    subject12_epoch1_input.npy
  vendor/utils/NOE_Engine.py
  noe-venv/bin/python
  noe-venv/lib/python3.11/site-packages/
```

在板端仓库根目录，将仓库里的 worker、冻结预处理和参考清单按原始字节复制到资产目录。V0.3.1 包装器由启动器直接从仓库读取。

```bash
ASSETS="$HOME/cix-demo-assets"
DEPTH_SRC=edge-platform/experiments/depth_posture_v03
mkdir -p "$ASSETS/depth/frozen"
cp "$DEPTH_SRC/run_npu.py" "$ASSETS/depth/"
cp "$DEPTH_SRC/frozen/"{reference.json,preprocess_v02.py} "$ASSETS/depth/frozen/"
```

## 3. 云端 PPG

在运行云端 Compose 的 PC 上，先初始化私有凭据。脚本拒绝覆盖已有 `.env`，有现成云端实例时可直接使用已登记的设备和操作者。Compose 启动 PostgreSQL 16、FastAPI 和 Vue，API 容器启动时运行 Alembic。

```bash
python cloud-platform/backend/scripts/init_dev.py
(cd cloud-platform && docker compose up --build -d)
```

云 Vue 默认访问 `http://127.0.0.1:5174`，云 API 监听 PC 回环 `8002`。云端和浏览器在同一台 PC 时，从该 PC 建立一次双向转发，使浏览器访问端侧服务，也使板端能访问云 API。

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 8001:127.0.0.1:18551 \
  -R 127.0.0.1:18002:127.0.0.1:8002 \
  orangepi@BOARD_HOST
```

板端此时使用 `EDGE_CLOUD_URL=http://127.0.0.1:18002`。如果云端已有板端可达的 HTTPS 地址，改用该地址并省略 `-R`。只演示静态深度时可跳过云端和反向转发，并省略下节的云端变量。

## 4. 板端启动 Qwen 和 FastAPI

在板端另开终端，从仓库根目录运行。NOE venv 如果已安装到其他目录，将其解释器和 `site-packages` 路径替换成实际值。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r edge-platform/backend/requirements.txt

ASSETS="$HOME/cix-demo-assets"
export CIX_DEMO_STATE_DIR="$HOME/.local/state/cix-demo"
export CIX_QWEN_MODEL="$ASSETS/qwen/Qwen3-4B-Q4_K_M.gguf"
export EDGE_DEPTH_ASSETS="$ASSETS/depth"
export EDGE_DEPTH_NPU_PYTHON="$ASSETS/noe-venv/bin/python"
export CIX_NOE_SITE_PACKAGES="$ASSETS/noe-venv/lib/python3.11/site-packages"
export CIX_NOE_HELPER_ROOT="$ASSETS/vendor"

# 演示云端 PPG 时配置以下变量
export EDGE_CLOUD_URL=http://127.0.0.1:18002
export EDGE_DEVICE_ID=mock-device-001
export EDGE_SLEEP_STAGE_ASSETS="$ASSETS/ppg"
read -rsp 'Cloud device token: ' EDGE_CLOUD_TOKEN; echo
export EDGE_CLOUD_TOKEN

bash edge-platform/deploy/start-demo.sh
```

状态目录须由当前用户拥有，权限为 700。启动器会以此权限创建缺失的状态目录。默认示例设备使用云主机 `cloud-platform/.env` 中的 `MOCK_DEVICE_TOKEN`。启动器设置 `EDGE_MOCK=0`，校验资产并启动板端回环端口 `18080` 的 llama-server 和 `18551` 的 FastAPI。若既有服务占用 `18551`，可用 `CIX_EDGE_PORT` 指定空闲端口并更新 SSH 正向转发的远端端口。确认已运行的 Qwen 使用同一哈希的权重后，可通过 `CIX_REUSE_QWEN=1` 复用。[启动脚本](../edge-platform/deploy/start-demo.sh)

## 5. 浏览器演示

在评审 PC 的新终端启动端侧 Vue，保留 SSH 转发。

```bash
cd edge-platform/frontend
npm ci
npm run dev
```

1. **静态深度姿态。** 打开 `http://127.0.0.1:5173`，使用板端启动器生成的 `$CIX_DEMO_STATE_DIR/operator.token` 或预先配置的 `EDGE_OPERATOR_TOKEN` 登录。打开 Workbench，提交“分析受试者 101 的第 8 个深度姿态样本并给出证据”。核对样本 `s101_p08`、Skill `depth_posture_analysis`、输出 `supine` 和 CIX NPU 执行证据。
2. **云端 PPG 闭环。** 打开 `http://127.0.0.1:5174`，使用云端 `cloud-platform/.operator-token.local` 中的操作者令牌登录。在 Agent 页选择已登记设备，点击 **提交 PPG 板端推理**。该按钮提交固定的 AAUWSS 受试者 12、epoch 1。待任务 `succeeded` 后点击 **查看结果**，核对 `CIX NOE NPU`、预测 `N1` 和关联结果事件。该样例人工标注为 `Wake`。

这两条任务在固定样例上运行。实时相机数据和新采集 PPG 尚未接入对应的 NPU Skill。[历史实机证据](hardware-evidence.md)

## 6. 可选的 Orbbec 预览

已有相机服务时，启动板端 API 前把 `EDGE_DEPTH_PREVIEW_PATH` 指向其 BMP。首次安装时，按 Orbbec 官方步骤安装 SDK 和 USB udev 规则，设置 `ORBBEC_SDK_ROOT`，用 CMake 和 C++17 编译器构建相机程序并安装用户服务。

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

默认 BMP 路径为 `$camera_home/live/depth.bmp`，端侧页面显示实时预览。
