"""Filesystem confinement and safe archive extraction (spec §19)."""
from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path


class PathViolation(Exception):
    pass


def confine(base: Path, candidate: str | Path) -> Path:
    """Resolve *candidate* relative to *base*; refuse anything escaping base."""
    base_r = Path(base).resolve()
    p = (base_r / candidate).resolve()
    if p != base_r and base_r not in p.parents:
        raise PathViolation(f"path escapes approved directory: {candidate!r}")
    return p


MAX_ARCHIVE_MEMBERS = 5000
MAX_ARCHIVE_UNCOMPRESSED = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


def safe_extract_zip(archive: Path, dest: Path) -> list[Path]:
    """Extract a zip, rejecting zip-slip, symlinks, absolute paths and zip bombs."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise PathViolation("archive has too many members")
        if sum(i.file_size for i in infos) > MAX_ARCHIVE_UNCOMPRESSED:
            raise PathViolation("archive expands beyond the permitted size")
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or ":" in name.split("/")[0]:
                raise PathViolation(f"absolute path in archive: {name!r}")
            if ((info.external_attr >> 16) & 0o170000) == stat.S_IFLNK:
                raise PathViolation(f"symlink in archive: {name!r}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise PathViolation(f"suspicious compression ratio: {name!r}")
            target = confine(dest, name)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as fh:
                fh.write(src.read())
            out.append(target)
    return out


def make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
