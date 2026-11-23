"""Verify a board-approved asset group with streaming SHA-256."""

import argparse
import hashlib
import json
from pathlib import Path


def main() -> int:
    groups = json.loads(Path(__file__).with_name("assets.json").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=groups)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    failed = False
    for name, expected in groups[args.group].items():
        path = args.root / name
        try:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            print(f"Cannot read {path}")
            failed = True
            continue
        if actual == expected:
            print(f"Verified {path}")
        else:
            print(f"SHA-256 mismatch: {path}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
