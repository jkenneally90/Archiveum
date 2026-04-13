#!/usr/bin/env bash

set -euo pipefail

python3 - <<'PY'
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
output = root / "archiveum_clean.zip"
exclude_dirs = {".git", "__pycache__", "archiveum_data", "temp", ".venv", "venv", "build", "dist"}
exclude_files = {".DS_Store"}
exclude_suffixes = {".pyc", ".pyo", ".log"}

if output.exists():
    output.unlink()

with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in root.rglob("*"):
        if path.is_dir():
            continue

        rel = path.relative_to(root)
        parts = rel.parts
        if any(part in exclude_dirs for part in parts):
            continue
        if path.name in exclude_files:
            continue
        if path.suffix.lower() in exclude_suffixes:
            continue
        if path.name.endswith("~"):
            continue

        archive.write(path, rel.as_posix())

print(f"Created clean zip: {output}")
PY
