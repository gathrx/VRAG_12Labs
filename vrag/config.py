"""Application settings loaded from `.env` (never exposed to the browser)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    api_key: str
    knowledge_store_id: str | None
    knowledge_store_name: str
    data_dir: Path
    request_timeout: int
    index_timeout: int

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def metadata_file(self) -> Path:
        return self.data_dir / "metadata.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "app.log"


def load_settings() -> Settings:
    data_dir = Path(os.getenv("VRAG_DATA_DIR", "").strip() or PROJECT_ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    settings = Settings(
        api_key=os.getenv("TWELVELABS_API_KEY", "").strip(),
        knowledge_store_id=os.getenv("TWELVELABS_KNOWLEDGE_STORE_ID", "").strip() or None,
        knowledge_store_name=os.getenv("TWELVELABS_KNOWLEDGE_STORE_NAME", "").strip() or "vrag-streamlit",
        data_dir=data_dir,
        request_timeout=_int_env("TWELVELABS_REQUEST_TIMEOUT", 600),
        index_timeout=_int_env("TWELVELABS_INDEX_TIMEOUT", 7200),
    )
    for folder in (settings.data_dir, settings.videos_dir, settings.frames_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return settings


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger("vrag")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(settings.log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
