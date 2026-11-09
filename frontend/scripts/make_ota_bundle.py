#!/usr/bin/env python3
"""Build a simple self-hosted Capgo-compatible WebView bundle ZIP.

Files live at the archive root (index.html MUST be a root member). This is
used only for JavaScript/CSS/HTML releases, not Android/iOS native binaries.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile


def make_bundle(source: Path, target: Path) -> dict:
    source = source.resolve(strict=True)
    if not source.is_dir() or not (source / "index.html").is_file():
        raise ValueError("Vite dist must contain index.html at bundle root")
    paths = sorted(p for p in source.rglob("*") if p.is_file())
    if not paths:
        raise ValueError("Empty bundle")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in paths:
            if file.is_symlink():
                raise ValueError("Symlinks are not supported in OTA bundles")
            relative = file.relative_to(source)
            if ".." in relative.parts:
                raise ValueError("Unsafe path")
            archive.write(file, relative.as_posix())

    with zipfile.ZipFile(target) as check:
        names = check.namelist()
        if "index.html" not in names or len(names) != len(set(names)):
            raise ValueError("Broken archive")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise ValueError("Archive paths are unsafe")
    size = target.stat().st_size
    if size < 1000 or size > 40 * 1024 * 1024:
        raise ValueError("Bundle size outside safety limits")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"file": str(target), "size": size, "checksum": digest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = make_bundle(args.source, args.output)
    print(json.dumps(result))
