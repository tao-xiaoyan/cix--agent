# CIX Demo 资产与依赖

板端资产哈希已实算。文件应放在 Git 之外的评审资产目录，再用 `python edge-platform/deploy/verify-assets.py 组名 资产根目录` 检查。机器清单见 [assets.json](../edge-platform/deploy/assets.json)。

| 组 | 版本、取得方式与相对路径 |
|---|---|
| `qwen` | Qwen 官方 GGUF 发布的 Q4_K_M，2,497,280,256 字节。发布页 SHA-256 与板端一致。将 `Qwen3-4B-Q4_K_M.gguf` 放至指定模型目录。[固定版本文件页](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf) |
| `depth` | CIX P1 V0.3 图 `depth_posture_v03.cix`（328,544 字节），以及同目录 `run_npu.py` 和 `frozen/{raw.npy,inputs.npy,reference.json,preprocess_v02.py}`。由有授权的维护者提供模型和冻结样本。若走重建路线，另需原版 V0.2 `artifacts/sha256.json`、V0.2 ONNX 和获授权的 SLP 派生输入。将它们放到独立 checkout 的预期位置后运行 V0.3 `prepare.py freeze --data <授权输入>`，比对最终冻结文件。实际可分发资产入口仍待维护者确认。 |
| `depth-interface` | Git 候选的 `edge-platform/experiments/depth_posture_v031/depth_posture_infer.py`。使用清单中的固定字节，由启动器默认从 checkout 引用。 |
| `ppg` | `sleep_cnn.cix`（205,492 字节），`epoch_source.json`（262 字节），和授权 `subject12_epoch1_input.npy`（7,808 字节）。对应 AAUWSS 30 秒、64 Hz、float32 `[1,1,1920]`，云请求为 `aauwss/12/1`。由有授权的项目维护者提供资产入口。发布候选使用已验证的编译产物。 |
| `noe-helper` | 厂商 `utils/NOE_Engine.py`，板端原在 `cix_npu_test/vision`。从 CIX 板卡对应示例包或经授权的供应渠道取得，以机器清单核验。精确的公开包入口待确认。 |

## 板卡依赖

- OS 和驱动取自 Orange Pi 的 CIX ARM64 协同镜像。板端核验为 Orange Pi 1.0.2 Bookworm、内核 `6.6.89-cix`、`cix-npu-driver 1.0.0+2503.orangepi`、`cix-noe-umd 2.0.2`，`/dev/aipu` 存在。系统 `/usr/share/cix/lib/libnoe.so.0.6.0` 的 SHA-256 为 `9021ff309a052c435a2d6905a2dd7c1f7603a29305e1b692949dc0b011f51aeb`。
- NOE Python 绑定为 ARM64 CPython 3.11 扩展，包版本 `libnoe 2.0.0`，板端 NPU venv 使用 NumPy `1.26.4`。扩展 `libnoe.cpython-311-aarch64-linux-gnu.so` 的 SHA-256 为 `1c26c5704c913c03ba65a4fe61c3522fa6d11bb954280e733e4043b8bd579415`。按板卡配套运行环境取得并安装在 NOE venv。CIX 主机上的 Qwen 可执行文件由 `cix-llama-cpp 1.2.4` 提供，路径 `/usr/share/cix/bin/llama-server`，SHA-256 为 `b33cc0492c5aa9db63074ef9cfa6ba71d6c65d832f59482f79254df7887881f2`。
- Orbbec Gemini 335 使用 Orbbec SDK v2.10.6 ARM64 发行包 `OrbbecSDK_v2.10.6_202609291004_575347c_linux_arm64.tar.gz` 与 `99-obsensor-libusb.rules`。解包库 `lib/libOrbbecSDK.so.2.10.6` 的板端 SHA-256 为 `319c9fd4b1a02bca231953545801ee0e957bff537a9c16d822632e4a5db53dde`。CMake 的 `OrbbecSDK_DIR` 指向发行包的 `lib`，运行时保留 SDK 扩展目录。[Orbbec 官方发行版](https://github.com/orbbec/OrbbecSDK_v2/releases)
- 板上 Agent Python 为 3.11.2，观察到 FastAPI 0.162.0、Uvicorn 0.54.0、Pydantic 2.14.2 和 HTTPX 0.28.1。API 的 Python 依赖在两个平台各自的 `backend/requirements.txt`。云镜像声明 Python 3.12 与 PostgreSQL 16。评审电脑使用 Node 22，Windows 候选构建使用 22.23.4；npm 依赖由两个前端的 `package-lock.json` 固定。
