#!/usr/bin/env python3
"""Stdlib bundle inventory, archive inspection and inside-out codesigning helpers."""
from __future__ import annotations

import argparse
import io
from pathlib import Path, PurePosixPath
import plistlib
import struct
import subprocess
import sys
import zipfile

# Both endian variants of 32/64-bit thin and universal Mach-O headers.
THIN = {b"\xfe\xed\xfa\xce": ">", b"\xce\xfa\xed\xfe": "<",
        b"\xfe\xed\xfa\xcf": ">", b"\xcf\xfa\xed\xfe": "<"}
FAT = {b"\xca\xfe\xba\xbe": (">", False), b"\xbe\xba\xfe\xca": ("<", False),
       b"\xca\xfe\xba\xbf": (">", True), b"\xbf\xba\xfe\xca": ("<", True)}
MACHO_MAGICS = THIN.keys() | FAT.keys()
ZIP_MAGICS = {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}
BUNDLE_SUFFIXES = {".app", ".framework", ".bundle", ".xpc", ".appex", ".plugin"}


def archive_machos(source, label: str, depth: int = 0) -> list[str]:
    """Find native binaries even in nested zips or entries without native extensions."""
    if depth > 8:
        raise ValueError(f"archive nesting too deep to inspect safely: {label}")
    found = []
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            name = f"{label}!{entry.filename}"
            with archive.open(entry) as stream:
                magic = stream.read(4)
                stream.seek(0)
                if magic in MACHO_MAGICS:
                    found.append(name)
                elif (magic in ZIP_MAGICS or entry.filename.lower().endswith(".zip")
                      or zipfile.is_zipfile(stream)):
                    if entry.file_size >= 300_000_000:
                        raise ValueError(f"nested archive too large to inspect: {name}")
                    stream.seek(0)
                    found.extend(archive_machos(io.BytesIO(stream.read()), name, depth + 1))
    return found


def bundle_paths(app: Path) -> list[Path]:
    """Do not follow framework aliases; reject links escaping the app before signing."""
    paths = list(app.rglob("*"))
    root = app.resolve()
    for path in paths:
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError(f"bundle symlink escapes the app: {path}")
    return [path for path in paths if not path.is_symlink()]


def scan_bundle(app: Path) -> None:
    found = []
    for path in bundle_paths(app):
        if path.is_file():
            with path.open("rb") as stream:
                magic = stream.read(4)
            if magic in ZIP_MAGICS or path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
                found.extend(archive_machos(path, str(path)))
    if found:
        raise ValueError("Mach-O hidden inside zip; run scripts/strip-bundle.py on the app "
                         "before smoke testing to remove .dSYM debug bundles. If native code "
                         "remains, rebuild with native modules unpacked before signing "
                         "(notarization inspects archives):\n" + "\n".join(found))


def check_zip(path: Path) -> None:
    """Check the single app root and safe member names before ditto extracts anything."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if not names:
            raise ValueError("empty app archive")
        for name in names:
            parts = PurePosixPath(name).parts
            if (not parts or parts[0] != "AI Guest List.app" or ".." in parts
                    or "\\" in name or name.startswith("/")):
                raise ValueError(f"archive must have exactly one top-level AI Guest List.app: {name}")


def macho_executable(stream, offset: int = 0) -> bool:
    """Read MH_EXECUTE from thin files or any slice of a universal executable."""
    stream.seek(offset)
    header = stream.read(16)
    magic = header[:4]
    if magic in THIN:
        return struct.unpack(THIN[magic] + "I", header[12:16])[0] == 2
    if magic in FAT:
        endian, wide = FAT[magic]
        count = struct.unpack(endian + "I", header[4:8])[0]
        if count > 64:
            raise ValueError("invalid universal Mach-O architecture count")
        for index in range(count):
            stream.seek(offset + 8 + index * (32 if wide else 20) + 8)
            slice_offset = struct.unpack(endian + ("Q" if wide else "I"),
                                         stream.read(8 if wide else 4))[0]
            stream.seek(offset + slice_offset)
            slice_header = stream.read(16)
            slice_magic = slice_header[:4]
            if slice_magic in THIN and struct.unpack(THIN[slice_magic] + "I", slice_header[12:16])[0] == 2:
                return True
    return False


def bundle_executable(bundle: Path) -> bool:
    # Frameworks contain dylibs, not host processes. Other nested bundles may launch CPython.
    for relative in ("Contents/Info.plist", "Resources/Info.plist", "Info.plist"):
        plist = bundle / relative
        if plist.is_file():
            with plist.open("rb") as stream:
                return plistlib.load(stream).get("CFBundlePackageType") in {"APPL", "XPC!"}
    return False


def sign_bundle(app: Path, identity: str, entitlements: Path, *, adhoc: bool = False) -> None:
    scan_bundle(app)  # Refuse before changing any signatures.
    paths = bundle_paths(app)
    files = []
    for path in paths:
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) in MACHO_MAGICS:
                    files.append(path)
    if not files:
        raise ValueError("no Mach-O files found in app")
    bundles = [path for path in paths if path.is_dir() and path.suffix in BUNDLE_SUFFIXES]
    deepest = lambda path: (-len(path.parts), str(path))
    # Every binary first, then nested containers deepest first, then the outer app LAST.
    for path in sorted(files, key=deepest) + sorted(bundles, key=deepest) + [app]:
        command = ["codesign", "--force", "--timestamp=none" if adhoc else "--timestamp",
                   "--options", "runtime", "--sign", identity]
        if path.is_file():
            with path.open("rb") as stream:
                needs_entitlements = macho_executable(stream)
        else:
            needs_entitlements = path == app or bundle_executable(path)
        if needs_entitlements:
            command += ["--entitlements", str(entitlements)]
        command.append(str(path))
        print(f"Signing: {path.relative_to(app.parent)}", flush=True)
        subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-zip")
    check.add_argument("zip", type=Path)
    scan = commands.add_parser("scan")
    scan.add_argument("app", type=Path)
    sign = commands.add_parser("sign")
    sign.add_argument("app", type=Path)
    sign.add_argument("--identity", required=True)
    sign.add_argument("--entitlements", required=True, type=Path)
    sign.add_argument("--adhoc", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check-zip":
            check_zip(args.zip)
        elif args.command == "scan":
            scan_bundle(args.app)
        else:
            sign_bundle(args.app, args.identity, args.entitlements, adhoc=args.adhoc)
    except (OSError, ValueError, struct.error, zipfile.BadZipFile,
            plistlib.InvalidFileException, subprocess.CalledProcessError) as exc:
        print(f"release bundle failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
