"""Download a YouTube video locally with yt-dlp.

TwelveLabs URL uploads do not accept video hosting platforms, and frames are extracted from a local
file, so YouTube links are downloaded first and then go through the normal upload/index workflow.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yt_dlp

log = logging.getLogger("vrag.youtube")

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)[\w-]{6,}",
    re.IGNORECASE,
)
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # matches the app's 2 GB upload ceiling


class YouTubeDownloadError(Exception):
    pass


@dataclass
class DownloadedVideo:
    path: Path
    title: str
    video_id: str
    duration_sec: float | None
    webpage_url: str


def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_RE.match(url.strip()))


def _js_runtimes() -> dict[str, dict]:
    # YouTube needs a JavaScript runtime for some formats; yt-dlp enables only Deno by default.
    runtimes: dict[str, dict] = {}
    for name in ("deno", "node", "bun"):
        if shutil.which(name):
            runtimes[name] = {}
    return runtimes


def download_youtube(url: str, out_dir: Path, basename: str,
                     on_progress: Callable[[float | None, str], None] | None = None) -> DownloadedVideo:
    """Download the best MP4 (≤1080p) to `out_dir/<basename>.mp4`."""
    url = url.strip()
    if not is_youtube_url(url):
        raise YouTubeDownloadError("That does not look like a YouTube video link.")
    out_dir.mkdir(parents=True, exist_ok=True)
    has_ffmpeg = bool(shutil.which("ffmpeg"))

    def hook(d: dict) -> None:
        if not on_progress:
            return
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = done / total if total else None
            mb = done / 1024 / 1024
            on_progress(frac, f"Downloading from YouTube… {mb:.1f} MB" + (f" ({frac:.0%})" if frac else ""))
        elif d.get("status") == "finished":
            on_progress(1.0, "Download finished, preparing file…")

    opts = {
        # Separate streams need FFmpeg to merge; otherwise take the best single progressive MP4.
        "format": ("bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/b[ext=mp4]/b"
                   if has_ffmpeg else "b[ext=mp4]/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"{basename}.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "retries": 5,
        "fragment_retries": 5,
    }
    runtimes = _js_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise YouTubeDownloadError("YouTube returned no video information.")
            if info.get("is_live"):
                raise YouTubeDownloadError("Live streams cannot be downloaded. Try again after the stream ends.")
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).replace("ERROR: ", "")
        raise YouTubeDownloadError(f"YouTube download failed: {msg}") from exc

    # After merging, the final file is .mp4 even if prepare_filename reported another extension.
    if not path.exists():
        candidates = sorted(out_dir.glob(f"{basename}.*"), key=lambda p: p.stat().st_size, reverse=True)
        candidates = [p for p in candidates if p.suffix not in (".part", ".ytdl")]
        if not candidates:
            raise YouTubeDownloadError("The download finished but the video file was not found "
                                       "(it may exceed the 2 GB limit).")
        path = candidates[0]

    return DownloadedVideo(
        path=path,
        title=info.get("title") or info.get("id") or "YouTube video",
        video_id=info.get("id") or "",
        duration_sec=info.get("duration"),
        webpage_url=info.get("webpage_url") or url,
    )
