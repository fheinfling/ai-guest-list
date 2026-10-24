#!/usr/bin/env python3
"""Remove debug-symbol bundles from a py2app app before smoke testing and signing."""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import zipfile


def is_dsym_member(entry: zipfile.ZipInfo) -> bool:
    # Only directory components count: a file merely named "symbols.dSYM" stays.
    return any(part.endswith(".dSYM") for part in entry.filename.split("/")[:-1])


def strip_zip(path: Path) -> None:
    """Rewrite only affected archives, replacing each atomically on the same volume."""
    with zipfile.ZipFile(path) as source:
        removed = [entry for entry in source.infolist() if is_dsym_member(entry)]
        if not removed:
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with zipfile.ZipFile(temporary, "w") as target:
                target.comment = source.comment
                for entry in source.infolist():
                    if is_dsym_member(entry):
                        continue
                    # Copy metadata, including compression method, timestamp, member
                    # comment, extra fields and attributes, in original member order.
                    with source.open(entry) as reader, target.open(copy.copy(entry), "w") as writer:
                        shutil.copyfileobj(reader, writer)
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    for entry in removed:
        print(f"Removed: {path}!{entry.filename}")


def strip_bundle(app: Path) -> None:
    if app.is_symlink() or not app.is_dir():
        raise ValueError(f"expected an app directory, not a symlink: {app}")
    lib = app / "Contents/Resources/lib"
    # Never follow bundle aliases or symlinks into files outside the app.
    for root, directories, files in os.walk(app, followlinks=False):
        parent = Path(root)
        for name in sorted(directories):
            path = parent / name
            if path.is_symlink():
                directories.remove(name)
            elif name.endswith(".dSYM"):
                shutil.rmtree(path)
                directories.remove(name)
                print(f"Removed: {path}/")
        if parent.is_relative_to(lib):
            for name in sorted(files):
                path = parent / name
                if path.suffix.lower() == ".zip" and not path.is_symlink():
                    strip_zip(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    try:
        strip_bundle(args.app)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"strip bundle failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
