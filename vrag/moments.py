"""Turn Jockey's `video_citation` annotations into timestamped result moments."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .twelvelabs_service import AnswerPart, SearchClip

MARKER_RE = re.compile(r"\s*\[\d+\]")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+|\n+")
NO_MATCH_PREFIX = "NO_MATCH"

SOURCE_JOCKEY = "jockey_citation"
SOURCE_SEARCH = "knowledge_store_search"


@dataclass
class Moment:
    start_sec: float
    end_sec: float
    description: str
    source: str
    markers: list[str]
    verified: bool = True        # range checked against the local file's duration
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def strip_ksi(item_id: str | None) -> str:
    return (item_id or "").removeprefix("ksi_")


def format_ts(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_range(start: float, end: float) -> str:
    return f"{format_ts(start)}–{format_ts(end)}"


def is_no_match(text: str) -> bool:
    return text.lstrip().upper().startswith(NO_MATCH_PREFIX)


def clean_no_match(text: str) -> str:
    stripped = text.lstrip()
    if stripped.upper().startswith(NO_MATCH_PREFIX):
        stripped = stripped[len(NO_MATCH_PREFIX):].lstrip(" :-—")
    return stripped.strip()


def _sentence_around(text: str, start: int, end: int) -> str:
    """Return the sentence that contains the cited span [start, end]."""
    left = 0
    for m in SENTENCE_END_RE.finditer(text, 0, start):
        left = m.end()
    right_match = SENTENCE_END_RE.search(text, end + 1)
    right = right_match.start() if right_match else len(text)
    sentence = _clean(text[left:right])
    if not sentence and left > 0:
        # Marker placed after the sentence end ("... a red car. [1]"): use the previous sentence.
        return _sentence_around(text, max(left - 2, 0), max(left - 2, 0))
    return sentence


def _clean(sentence: str) -> str:
    sentence = MARKER_RE.sub("", sentence)
    sentence = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", sentence)  # list bullets / numbering
    sentence = re.sub(r"\*\*|__|^#+\s*", "", sentence)           # markdown emphasis / headings
    return sentence.strip()


def _clamp(start: float, end: float, duration: float | None) -> tuple[float, float, bool, str | None]:
    """Validate a range against the real video length."""
    if end < start:
        start, end = end, start
    if not duration:
        return start, end, False, "Video duration unknown; range not verified locally."
    if start >= duration:
        return start, end, False, "Cited start time is beyond the end of the video."
    note = None
    if end > duration + 0.5:
        note = "End time clamped to the video length."
    return max(0.0, start), min(end, duration), True, note


def moments_from_citations(parts: list[AnswerPart], item_id: str,
                           duration: float | None) -> tuple[list[Moment], bool]:
    """Extract ranged video citations for `item_id`.

    Returns (moments, cited_whole_video). `cited_whole_video` is True when Jockey cited the video
    without a time range, which means a clip must be located another way.
    """
    target = strip_ksi(item_id)
    by_range: dict[tuple[float, float], Moment] = {}
    whole_video = False
    for part in parts:
        for ann in part.annotations:
            if ann.get("type") != "video_citation" or strip_ksi(ann.get("item_id")) != target:
                continue
            if ann.get("start_sec") is None or ann.get("end_sec") is None:
                whole_video = True
                continue
            start, end, verified, note = _clamp(float(ann["start_sec"]), float(ann["end_sec"]), duration)
            if not verified and note and "beyond" in note:
                continue  # never show a frame for a range that does not exist in the video
            s_idx, e_idx = int(ann.get("start_index", 0)), int(ann.get("end_index", 0))
            marker = part.text[s_idx:e_idx + 1].strip()
            description = _sentence_around(part.text, s_idx, e_idx)
            key = (round(start, 1), round(end, 1))
            if key in by_range:
                existing = by_range[key]
                if description and description not in existing.description:
                    existing.description = f"{existing.description} {description}".strip()
                if marker and marker not in existing.markers:
                    existing.markers.append(marker)
            else:
                by_range[key] = Moment(start, end, description, SOURCE_JOCKEY,
                                       [marker] if marker else [], verified, note)
    moments = sorted(by_range.values(), key=lambda m: (m.start_sec, m.end_sec))
    return moments, whole_video


def moments_from_search(clips: list[SearchClip], duration: float | None, question: str) -> list[Moment]:
    moments: list[Moment] = []
    for clip in clips:
        start, end, verified, note = _clamp(clip.start_sec, clip.end_sec, duration)
        if not verified and note and "beyond" in note:
            continue
        desc = (f"Transcript: “{clip.transcription.strip()}”" if clip.transcription
                else f"Rank {clip.rank} visual/audio match for “{question.strip()}”.")
        moments.append(Moment(start, end, desc, SOURCE_SEARCH, [], verified,
                              note or "Located with knowledge store search (Jockey gave no time range)."))
    return moments


def render_answer(parts: list[AnswerPart], item_id: str) -> str:
    """Answer text with each `[n]` media marker replaced by its timestamp range."""
    target = strip_ksi(item_id)
    rendered: list[str] = []
    for part in parts:
        text = part.text
        spans = []
        for ann in part.annotations:
            if ann.get("type") != "video_citation" or strip_ksi(ann.get("item_id")) != target:
                continue
            s, e = int(ann.get("start_index", 0)), int(ann.get("end_index", 0))
            if ann.get("start_sec") is not None and ann.get("end_sec") is not None:
                label = f" `{format_range(float(ann['start_sec']), float(ann['end_sec']))}`"
            else:
                label = " `whole video`"
            spans.append((s, e, label))
        # Replace right-to-left so earlier offsets stay valid; skip overlapping spans.
        last_start = len(text) + 1
        for s, e, label in sorted(spans, key=lambda x: x[0], reverse=True):
            if e >= last_start or not text[s:e + 1].strip().startswith("["):
                continue
            text = text[:s].rstrip() + label + text[e + 1:]
            last_start = s
        rendered.append(MARKER_RE.sub("", text) if not spans else text)
    return "\n\n".join(rendered).strip()
