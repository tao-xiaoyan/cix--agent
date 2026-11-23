# CIX Demo 资产与依赖

资产放在 Git 仓库之外，目录见[最小复现说明](demo-reproduction.md#2-资产目录)。启动器使用 [assets.json](../edge-platform/deploy/assets.json) 校验文件的原始字节。也可单独运行 `python edge-platform/deploy/verify-assets.py GROUP ASSET_DIR`。待授权的 `.cix` 和 SLP 冻结数组由维护者确认交付权限后提供。

## 官方下载

| 文件 | 固定来源 | SHA-256 |
| --- | --- | --- |
| `Qwen3-4B-Q4_K_M.gguf` | Qwen 官方提交 `bc640142c66e1fdd12af0bd68f40445458f3869b`，Apache-2.0。[直接下载](https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf) · [版本与许可](https://huggingface.co/Qwen/Qwen3-4B-GGUF/blob/bc640142c66e1fdd12af0bd68f40445458f3869b/Qwen3-4B-Q4_K_M.gguf) | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| `utils/NOE_Engine.py` | CIX Model Hub **25Q3**，与板端 10,343 字节的文件一致，文件头标注 Apache-2.0。[精确匹配版本下载](https://www.modelscope.cn/api/v1/models/cix/ai_model_hub_25_Q3/repo?Revision=master&FilePath=utils%2FNOE_Engine.py) | `5b5e0a06014b1689cebd5736f2bb68767e2c83f319402b43a7a94236675f536b` |

## CIX 模型与分发状态

| 文件 | 生成来源 | SHA-256 | 交付状态 |
| --- | --- | --- | --- |
| `sleep_cnn.cix` | 项目 1D CNN 在 AAUWSS 训练子集上从头训练，导出 ONNX，由 CixBuilder 26Q2 编译为 CIX P1 图。 | `f24597fe2ec04170e0684e9c4266b7b05802dde3901833ed5264456c8ef92854` | **待授权交付。** 需确认团队对模型的分发权、数据署名义务和所用 26Q2 工具条款对产物交付的适用范围。 |
| `depth_posture_v03.cix` | 项目 V0.2 CNN 在 SLP 薄被深度子集上训练，经过 ONNX 导出和量化校准，由 CixBuilder 26Q2 编译。 | `ad33423c78e738a9f5f14c1a4e4195725f41c4eab7bd76323fde6a73a10a7478` | **待授权交付。** 需确认 ACLab 是否许可赛事评审使用及衍生模型分发，并核实 26Q2 工具条款。 |

两个文件均为已验收板端产物。当前无获准的公开下载地址。授权确认后，由维护者按许可范围提供对应原件和上述校验值。[SLP 数据条款](https://github.com/ostadabbas/SLP-Dataset-and-Code#license) · [CIX 开发者 EULA](https://developer.cixtech.com/userAgreement)

## AAUWSS 固定 epoch

AAUWSS 1.1 的 DOI 为 `10.5281/zenodo.16919071`，数据许可为 CC BY 4.0。使用时署名 Djanian、Nielsen、Nielsen 和 Bruun，附数据 DOI 与许可，并说明样例从原始数据提取而来。Zenodo 存档同时要求引用配套论文。[官方存档](https://doi.org/10.5281/zenodo.16919071) · [许可元数据](https://zenodo.org/api/records/16919071) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) · [配套论文](https://doi.org/10.1088/1361-6579/ae5458)

可在 PC 上从官方 ZIP 提取已验证的单个 epoch。使用 NumPy 2.1.3 和 pandas 2.2.3。设 `AAUWSS_ZIP` 为本地官方 ZIP 路径，`ASSETS` 为本地资产根目录。从仓库根运行以下命令，再将生成的两个文件放入板端资产目录的 `ppg/`。脚本先核对官方归档中的 pickle 成员哈希，再读取第 0 行，仅导出该行的 1920 个信号值和溯源字段。

```bash
python - "$AAUWSS_ZIP" "$ASSETS/ppg" <<'PY'
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

manifest = json.loads(Path("edge-platform/deploy/assets.json").read_text())["ppg"]
with zipfile.ZipFile(sys.argv[1]) as archive:
    source = archive.read("aligned_sleep_data_set/ppg/subject_12_ppg.pkl")
source_sha = hashlib.sha256(source).hexdigest()
if source_sha != "39b4bc931f346d2fed8102502451e1f1d6d747538674e42e11dc30276f0e2d13":
    raise SystemExit("AAUWSS source pickle hash mismatch")

table = pd.read_pickle(io.BytesIO(source))
row = table.iloc[0]
label, epoch = str(row["labels"]), float(row["epoch"])
if (label, epoch) != ("Wake", 1.0):
    raise SystemExit("Unexpected AAUWSS label or epoch")

samples = table.loc[:, list(range(1920))].iloc[0].to_numpy(dtype="<f4")
stream = io.BytesIO()
np.save(stream, samples.reshape(1, 1, 1920), allow_pickle=False)
npy = stream.getvalue()
meta = {
    "input_npy_sha256": hashlib.sha256(npy).hexdigest(),
    "source_pickle_sha256": source_sha,
    "row": 0,
    "true_label": label,
    "subject": 12,
    "epoch": epoch,
}
outputs = {
    "subject12_epoch1_input.npy": npy,
    "epoch_source.json": json.dumps(meta, indent=2).replace("\n", "\r\n").encode("utf-8"),
}
for name, content in outputs.items():
    if hashlib.sha256(content).hexdigest() != manifest[name]:
        raise SystemExit(f"Output hash mismatch: {name}")
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
if any((out / name).exists() for name in outputs):
    raise SystemExit("Output sample files already exist")
for name, content in outputs.items():
    with (out / name).open("xb") as stream:
        stream.write(content)
PY
```

## SLP 冻结深度输入

数据源为 Northeastern ACLab 的 SLP 薄被深度模态。原始数据受官方非商业用途条款限制。冻结数组目前**待授权交付**，现有资料未确认与本项目清洗版字节一致的合法公开链接。评委可在取得许可后使用由维护者提供的两份冻结数组，或从已获授权且 SHA-256 匹配的 `depth_cover1_cleaned_0to102.npy` 重建。[SLP 官方数据条款](https://github.com/ostadabbas/SLP-Dataset-and-Code#license)

重建使用清洗版父文件 SHA-256 `a7d4e34aff50af1d0a5fd000de362f86c04889e8aaafad673c91f840c1f25eac`，形状 `(102,45,128,54)`、类型 `float32`。使用仓库中锁定的 [reference.json](../edge-platform/experiments/depth_posture_v03/frozen/reference.json) 和 [preprocess_v02.py](../edge-platform/experiments/depth_posture_v03/frozen/preprocess_v02.py)：

1. 按 `reference.json` 中 `samples` 的顺序取 `source[subject - 1, pose - 1]`，堆叠 66 帧并以 `np.save(..., allow_pickle=False)` 写为 `frozen/raw.npy`，形状 `(66,128,54)`。
2. 对上述各帧调用 `preprocess(frame)` 并按相同顺序堆叠，以同样方式写为 `frozen/inputs.npy`，形状 `(66,1,1,128,54)`。本步骤用 NumPy 2.1.3 重建得到与板端一致的文件哈希。
3. 保留仓库内的参考清单和预处理源码原始字节。配齐 `run_npu.py` 与模型后，运行 `python edge-platform/deploy/verify-assets.py depth "$ASSETS/depth"` 校验整个目录。

## 板卡依赖

- 已验收的 Orange Pi 6 Plus ARM64 环境为 Orange Pi 1.0.2 Bookworm、内核 `6.6.89-cix`、`cix-npu-driver 1.0.0+2503.orangepi` 和 `cix-noe-umd 2.0.2`。运行用户需要读写 `/dev/aipu`。厂商运行库及驱动使用板卡配套渠道。[CIX 开发者入口](https://developer.cixtech.com/)
- NOE venv 使用 ARM64 CPython 3.11、`libnoe` Python 绑定 `2.0.0` 和 NumPy `1.26.4`。CIX Qwen 服务使用 `cix-llama-cpp 1.2.4` 的 `/usr/share/cix/bin/llama-server`。板端 API 依赖见 [requirements.txt](../edge-platform/backend/requirements.txt)。
- 实时 BMP 预览另需 Gemini 335、Orbbec SDK `2.10.6`（Linux ARM64）及 USB udev 规则。[Orbbec 官方发行版](https://github.com/orbbec/OrbbecSDK_v2/releases)
