"""Read video properties and extract real frames from the original file (OpenCV, FFmpeg fallback)."""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2

log = logging.getLogger("vrag.frames")

MAX_FRAME_WIDTH = 960


@dataclass
class VideoInfo:
    duration_sec: float | None
    fps: float | None
    width: int | None
    height: int | None
    frame_count: int | None


def probe_video(path: Path) -> VideoInfo:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return _probe_ffprobe(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or None
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) or None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) or None
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) or None
        duration = frames / fps if fps and frames else None
    finally:
        cap.release()
    if duration is None:
        fallback = _probe_ffprobe(path)
        duration = fallback.duration_sec
    return VideoInfo(duration, fps, width, height, frames)


def _probe_ffprobe(path: Path) -> VideoInfo:
    ffprobe = shutil.which("ffprobe")
    duration = None
    if ffprobe:
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout.strip()
            duration = float(out) if out else None
        except (subprocess.SubprocessError, ValueError) as exc:
            log.warning("ffprobe failed for %s: %s", path, exc)
    return VideoInfo(duration, None, None, None, None)


def representative_time(start: float, end: float, duration: float | None) -> float:
    """Midpoint of the moment, kept safely inside the video."""
    t = start + max(end - start, 0) / 2
    if duration:
        t = min(t, max(duration - 0.1, 0))
    return max(t, 0.0)


def extract_frame(video_path: Path, seconds: float, out_path: Path) -> Path | None:
    """Save the frame at `seconds` as JPEG. Cached on disk; returns None if no frame could be read."""
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if _extract_opencv(video_path, seconds, out_path) or _extract_ffmpeg(video_path, seconds, out_path):
        return out_path
    log.error("Could not extract frame at %.2fs from %s", seconds, video_path)
    return None


def _resize(frame):
    h, w = frame.shape[:2]
    if w > MAX_FRAME_WIDTH:
        frame = cv2.resize(frame, (MAX_FRAME_WIDTH, int(h * MAX_FRAME_WIDTH / w)), interpolation=cv2.INTER_AREA)
    return frame


def _extract_opencv(video_path: Path, seconds: float, out_path: Path) -> bool:
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps:
                return False
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(seconds * fps))
            ok, frame = cap.read()
        if not ok or frame is None:
            return False
        # cv2.imwrite cannot handle non-ASCII Windows paths; encode then write bytes instead.
        ok, buf = cv2.imencode(".jpg", _resize(frame), [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return False
        out_path.write_bytes(buf.tobytes())
        return True
    finally:
        cap.release()


def _extract_ffmpeg(video_path: Path, seconds: float, out_path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}",
             "-i", str(video_path), "-frames:v", "1", "-vf", f"scale='min({MAX_FRAME_WIDTH},iw)':-2",
             "-q:v", "3", str(out_path)],
            capture_output=True, timeout=120, check=True,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except subprocess.SubprocessError as exc:
        log.warning("ffmpeg frame extraction failed: %s", exc)
        return False
