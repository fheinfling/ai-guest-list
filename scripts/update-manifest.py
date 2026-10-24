#!/usr/bin/env python3
"""Write the release/update contract using only the Python standard library."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sys

REPO_URL = "https://github.com/fheinfling/ai-guest-list"
BUNDLE_ID = "com.fheinfling.aiguestlist"
MAX_ZIP_SIZE = 300_000_000  # Contract: strictly less than 300 MB (decimal).
MAX_MANIFEST_SIZE = 16 * 1024
KEYS = {"schema", "version", "build", "published_at", "url", "sha256", "size",
        "minimum_system_version", "notes_url"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_app(app: Path, tag: str) -> dict:
    require(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag) is not None,
            "tag must be vX.Y.Z (decimal components only)")
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    require(isinstance(info, dict), "Info.plist must be a dictionary")
    require(info.get("CFBundleIdentifier") == BUNDLE_ID, "wrong CFBundleIdentifier")
    require(info.get("CFBundleShortVersionString") == tag[1:],
            "CFBundleShortVersionString must equal the tag version")
    build = info.get("CFBundleVersion")
    require(isinstance(build, str) and re.fullmatch(r"[0-9]+", build) is not None,
            "CFBundleVersion must be a decimal string")
    minimum = info.get("LSMinimumSystemVersion")
    require(isinstance(minimum, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", minimum)
            is not None, "LSMinimumSystemVersion must be a macOS version string")
    return info


def zip_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            require(size < MAX_ZIP_SIZE, "zip must be smaller than 300 MB")
            digest.update(block)
    require(size > 0, "zip must not be empty")
    return digest.hexdigest(), size


def validate_manifest(manifest: dict, *, tag: str, info: dict, zip_path: Path) -> None:
    """Validate independently of construction, including actual artifact hash/length."""
    require(set(manifest) == KEYS, "writer must emit exactly the contract keys")
    require(type(manifest["schema"]) is int and manifest["schema"] == 1, "schema must be integer 1")
    version = manifest["version"]
    require(isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            is not None, "invalid version")
    require(tag == f"v{version}" and info["CFBundleShortVersionString"] == version,
            "version must match tag and Info.plist")
    build = manifest["build"]
    require(isinstance(build, str) and re.fullmatch(r"[0-9]+", build) is not None
            and build == info["CFBundleVersion"], "invalid build")
    # Emit the canonical URL, stricter than the reader's prefix/suffix allowance.
    require(manifest["url"] == f"{REPO_URL}/releases/download/{tag}/AI-Guest-List-{tag}.zip",
            "invalid release download URL prefix or asset name")
    require(manifest["notes_url"] == f"{REPO_URL}/releases/tag/{tag}", "invalid notes_url")
    sha = manifest["sha256"]
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) is not None,
            "sha256 must be 64 lowercase hex characters")
    size = manifest["size"]
    require(type(size) is int and 0 < size < MAX_ZIP_SIZE, "invalid zip size")
    actual_sha, actual_size = zip_digest(zip_path)
    require(sha == actual_sha and size == actual_size, "zip sha256/size mismatch")
    require(manifest["minimum_system_version"] == info["LSMinimumSystemVersion"],
            "minimum_system_version must match Info.plist")
    published = manifest["published_at"]
    require(isinstance(published, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", published) is not None,
        "published_at must be a UTC timestamp")
    datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
    require(len(encode_manifest(manifest)) < MAX_MANIFEST_SIZE, "manifest must be smaller than 16 KB")


def encode_manifest(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def make_manifest(zip_path: Path, app: Path, tag: str) -> dict:
    info = read_app(app, tag)
    sha, size = zip_digest(zip_path)
    manifest = {
        "schema": 1,
        "version": tag[1:],
        "build": info["CFBundleVersion"],
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": f"{REPO_URL}/releases/download/{tag}/AI-Guest-List-{tag}.zip",
        "sha256": sha,
        "size": size,
        "minimum_system_version": info["LSMinimumSystemVersion"],
        "notes_url": f"{REPO_URL}/releases/tag/{tag}",
    }
    validate_manifest(manifest, tag=tag, info=info, zip_path=zip_path)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", required=True, type=Path, dest="zip_path")
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = make_manifest(args.zip_path, args.app, args.tag)
        args.out.write_bytes(encode_manifest(manifest))
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as exc:
        print(f"update manifest failed: {exc}", file=sys.stderr)
        return 1
    print(f"sha256: {manifest['sha256']}\nsize: {manifest['size']} bytes\n"
          f"zip: {manifest['url']}\nnotes: {manifest['notes_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
