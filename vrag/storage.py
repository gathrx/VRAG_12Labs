"""Persistent video metadata and chat history, stored as JSON on disk."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Processing stages, in order. `failed` can happen at any stage.
STATUS_SAVED = "saved"              # file stored locally, not uploaded yet
STATUS_UPLOADING = "uploading"      # upload to TwelveLabs in progress
STATUS_ASSET_PROCESSING = "asset_processing"
STATUS_INDEXING = "indexing"        # knowledge store item queued/pending/processing
STATUS_READY = "ready"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_SAVED: "Saved locally",
    STATUS_UPLOADING: "Uploading",
    STATUS_ASSET_PROCESSING: "Processing asset",
    STATUS_INDEXING: "Indexing",
    STATUS_READY: "Ready",
    STATUS_FAILED: "Failed",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MetadataStore:
    """Thread-safe JSON store: {"knowledge_store_id": str|None, "videos": {id: record}}."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        if not path.exists():
            self._write({"knowledge_store_id": None, "videos": {}})

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("knowledge_store_id", None)
        data.setdefault("videos", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic on Windows and POSIX

    # Knowledge store -------------------------------------------------------
    def get_knowledge_store_id(self) -> str | None:
        with self._lock:
            return self._read()["knowledge_store_id"]

    def set_knowledge_store_id(self, store_id: str | None) -> None:
        with self._lock:
            data = self._read()
            data["knowledge_store_id"] = store_id
            self._write(data)

    # Videos ----------------------------------------------------------------
    def list_videos(self) -> list[dict[str, Any]]:
        with self._lock:
            videos = list(self._read()["videos"].values())
        return sorted(videos, key=lambda v: v.get("created_at", ""), reverse=True)

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read()["videos"].get(video_id)

    def create_video(self, **fields: Any) -> dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex[:12],
            "status": STATUS_SAVED,
            "asset_id": None,
            "item_id": None,
            "knowledge_store_id": None,
            "session_id": None,
            "error": None,
            "chat": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        record.update(fields)
        with self._lock:
            data = self._read()
            data["videos"][record["id"]] = record
            self._write(data)
        return record

    def update_video(self, video_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            record = data["videos"][video_id]
            record.update(fields)
            record["updated_at"] = utc_now()
            self._write(data)
            return record

    def append_chat(self, video_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            record = data["videos"][video_id]
            record.setdefault("chat", []).append(message)
            record["updated_at"] = utc_now()
            self._write(data)

    def delete_video(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read()
            record = data["videos"].pop(video_id, None)
            self._write(data)
            return record
