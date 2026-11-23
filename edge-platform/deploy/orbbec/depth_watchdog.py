"""Restart the user capture service when its latest BMP is missing or stale."""

import os
from pathlib import Path
import subprocess
import time


def main() -> None:
    bmp = Path(os.environ.get("EDGE_DEPTH_PREVIEW_PATH", "live/depth.bmp"))
    try:
        age = time.time() - bmp.stat().st_mtime
    except FileNotFoundError:
        age = float("inf")

    if age > 12:
        print(f"Depth stale ({age:.1f}s). Restarting capture.", flush=True)
        subprocess.run(
            ["systemctl", "--user", "restart", "depth-camera.service"],
            timeout=20,
            check=True,
        )


if __name__ == "__main__":
    main()
