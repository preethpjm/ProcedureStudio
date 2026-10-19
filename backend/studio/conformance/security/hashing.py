from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_checksum(root: Path, files: list[str]) -> str:
    """Deterministic checksum over a set of files (relative paths, sorted)."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.replace("\\", "/").encode())
        h.update(b"\0")
        h.update(sha256_file(root / rel).encode())
        h.update(b"\n")
    return h.hexdigest()
