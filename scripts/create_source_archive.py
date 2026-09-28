#!/usr/bin/env python3
"""Build release sources from an explicit allowlist in a clean Git commit."""
from __future__ import annotations

import argparse
import io
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

ROOT_FILES = {
    "README.md", "LICENSE.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "MANIFEST.in", "kubengine.spec", "requirements.txt", "requirements-dev.txt",
}
SOURCE_DIRS = {"src", "scripts", "docs", "migrations", "static", "tests"}
TEMPLATE = "config/application.example.yaml"
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".token", ".db", ".sqlite", ".sqlite3"}


def release_path(path: str) -> bool:
    item = PurePosixPath(path)
    if any(part.startswith(".") or part == "__pycache__" for part in item.parts):
        return False
    if item.suffix.lower() in PRIVATE_SUFFIXES or item.name in {"id_rsa", "id_ed25519"}:
        return False
    return path == TEMPLATE or path in ROOT_FILES or item.parts[0] in SOURCE_DIRS


def create_archive(repo: Path, output: Path, prefix: str) -> None:
    if not prefix or "/" in prefix or prefix in {".", ".."}:
        raise ValueError("Archive prefix must be a single directory name")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(repo), *args])

    # Runtime configuration is deliberately excluded from the release. Source
    # changes must be committed, rather than silently releasing older code.
    dirty = git("diff", "--name-only", "-z", "HEAD", "--").split(b"\0")
    if any(release_path(path.decode()) for path in dirty if path):
        raise ValueError("Release sources have uncommitted changes; use a clean committed checkout")

    entries = []
    for entry in git("ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        path = name.decode()
        if not release_path(path):
            continue
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"Release source must be a regular tracked file: {path}")
        entries.append((path, mode, oid))
    if not any(path == TEMPLATE for path, _, _ in entries):
        raise ValueError(f"The committed release must contain {TEMPLATE}")

    timestamp = int(git("show", "-s", "--format=%ct", "HEAD").strip())
    with tarfile.open(output, "w:gz") as archive:
        for path, mode, oid in entries:
            data = git("cat-file", "blob", oid)
            member = tarfile.TarInfo(f"{prefix}/{path}")
            member.size = len(data)
            member.mode = 0o755 if mode == "100755" else 0o644
            member.mtime = timestamp
            archive.addfile(member, io.BytesIO(data))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    create_archive(args.repo, args.output, args.prefix)
