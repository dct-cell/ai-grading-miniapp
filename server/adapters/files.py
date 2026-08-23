from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


_CHUNK_BYTES = 1024 * 1024
_SAFE_FILE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

STAGING_DIRECTORY = "staging"
TEMPORARY_DIRECTORY = "temporary"


class FileStorageError(ValueError):
    pass


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    size_bytes: int
    sha256: str


def _require_safe_file_id(file_id: str) -> str:
    if not isinstance(file_id, str) or not _SAFE_FILE_ID.fullmatch(file_id):
        raise FileStorageError("文件标识不合法。")
    if file_id in {".", ".."}:
        raise FileStorageError("文件标识不合法。")
    return file_id


class LocalFileStore:
    """Server-local primary storage for order files."""

    def __init__(self, root: Path, *, max_bytes: int | None = None) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def resolve(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise FileStorageError("文件路径不合法。")
        candidate = Path(relative_path)
        if candidate.is_absolute() or "\x00" in relative_path:
            raise FileStorageError("文件路径不合法。")
        if any(part in {"..", ""} for part in candidate.parts):
            raise FileStorageError("文件路径不合法。")
        root = self.root.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise FileStorageError("文件路径不合法。")
        return resolved

    def put_temporary(self, file_id: str, source: BinaryIO) -> StoredFile:
        _require_safe_file_id(file_id)
        relative_path = f"{TEMPORARY_DIRECTORY}/{file_id}.pdf"
        staging = self.resolve(f"{STAGING_DIRECTORY}/{file_id}.part")
        target = self.resolve(relative_path)
        staging.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        try:
            with staging.open("wb") as output:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if self.max_bytes is not None and size > self.max_bytes:
                        raise FileStorageError(
                            f"文件超过 {self.max_bytes} 字节上限。"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staging, target)
        except FileStorageError:
            staging.unlink(missing_ok=True)
            raise
        except OSError as error:
            staging.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise FileStorageError("保存文件失败。") from error

        return StoredFile(
            relative_path=relative_path,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    def put_at(
        self,
        relative_path: str,
        source: BinaryIO,
        *,
        max_bytes: int | None = None,
    ) -> StoredFile:
        """Write a stream to an explicit relative path, atomically.

        Used for Worker result staging, where the caller owns the layout
        (result-staging/<job>/<lease_version>/...) rather than a flat file id.
        """
        target = self.resolve(relative_path)
        staging = self.resolve(
            f"{STAGING_DIRECTORY}/{uuid4().hex}.part"
        )
        staging.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        limit = self.max_bytes if max_bytes is None else max_bytes

        digest = hashlib.sha256()
        size = 0
        try:
            with staging.open("wb") as output:
                while True:
                    chunk = source.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if limit is not None and size > limit:
                        raise FileStorageError(f"文件超过 {limit} 字节上限。")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(staging, target)
        except FileStorageError:
            staging.unlink(missing_ok=True)
            raise
        except OSError as error:
            staging.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise FileStorageError("保存文件失败。") from error

        return StoredFile(
            relative_path=relative_path,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    def copy(self, source_relative_path: str, target_relative_path: str) -> None:
        """Copy a stored object within the storage root, atomically at the target.

        The bytes land in staging first and are then renamed into place, so a
        reader never observes a half-written target. The source is left alone;
        callers delete it only after their transaction has committed.
        """
        source = self.resolve(source_relative_path)
        target = self.resolve(target_relative_path)
        if not source.is_file():
            raise FileStorageError("待复制的文件不存在。")
        staging = self.resolve(f"{STAGING_DIRECTORY}/{uuid4().hex}.part")
        staging.parent.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as reader, staging.open("wb") as writer:
                while True:
                    chunk = reader.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(staging, target)
        except OSError as error:
            staging.unlink(missing_ok=True)
            raise FileStorageError("复制文件失败。") from error

    def delete(self, relative_path: str) -> None:
        self.resolve(relative_path).unlink(missing_ok=True)
