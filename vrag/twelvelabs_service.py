"""Thin, retrying wrapper around the official TwelveLabs Python SDK (Agents / Jockey).

Workflow (per https://docs.twelvelabs.io/v1.3/agents/get-started/quickstart):
    assets.create -> poll assets.retrieve until "ready"
    knowledge_stores.create (once)
    knowledge_store_items.create -> poll knowledge_store_items.retrieve until "ready"
    responses.create_stream (Jockey) -> answer text + video_citation annotations
    knowledge_stores.search (fallback for locating a clip when Jockey gives no time range)
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx
from twelvelabs import TwelveLabs
from twelvelabs.core.api_error import ApiError

log = logging.getLogger("vrag.twelvelabs")

T = TypeVar("T")

# Direct uploads accept local files up to 200 MB; larger files (up to 10 GB) use multipart.
DIRECT_UPLOAD_LIMIT = 200 * 1000 * 1000
MULTIPART_UPLOAD_LIMIT = 10 * 1000 * 1000 * 1000

ITEM_PENDING_STATES = {"queued", "pending", "processing"}

JOCKEY_INSTRUCTIONS = """You are a precise video moment finder. Answer questions about the selected video only.
Rules:
- Ground every statement in what is visible or audible in the video. Never guess or invent details.
- For every distinct moment that answers the question, cite that moment with its own citation so each
  citation covers one specific, tight time range. If the subject appears several times, cite every
  occurrence separately, in chronological order.
- For each moment, write one short sentence describing what happens in that scene, followed by its citation.
- If nothing in the video is relevant, reply with exactly "NO_MATCH:" followed by one sentence explaining
  what the video shows instead, and include no citations."""


class TwelveLabsServiceError(Exception):
    """A user-presentable error from the TwelveLabs API or the processing workflow."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def describe_api_error(exc: ApiError) -> str:
    body = exc.body if isinstance(exc.body, dict) else {}
    detail = body.get("message") or (exc.body if isinstance(exc.body, str) else "") or ""
    code = body.get("code")
    hints = {
        400: "The request was rejected as invalid.",
        401: "Authentication failed. Check TWELVELABS_API_KEY in your .env file.",
        403: "Your TwelveLabs account or plan does not have permission for this operation "
             "(Agents/Jockey access may need to be enabled for your account).",
        404: "The requested TwelveLabs resource was not found.",
        410: "The resource or page token has expired.",
        413: "The file is too large for this upload method.",
        422: "The request could not be processed.",
        429: "Rate limit reached. Please wait a moment and try again.",
    }
    status = exc.status_code
    base = hints.get(status, "TwelveLabs server error." if status and status >= 500 else "TwelveLabs API error.")
    extra = " ".join(p for p in (f"[{code}]" if code else "", str(detail)) if p).strip()
    return f"{base} (HTTP {status}) {extra}".strip()


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, ApiError):
        return exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500)
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(exc, "headers", None) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def with_retry(call: Callable[[], T], *, attempts: int = 4, base_delay: float = 2.0, what: str = "request") -> T:
    """Run `call`, retrying 429 / 5xx / network errors with exponential backoff and jitter."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised below when not retryable
            if not _is_retryable(exc) or attempt == attempts:
                if isinstance(exc, ApiError):
                    raise TwelveLabsServiceError(describe_api_error(exc), exc.status_code) from exc
                if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                    raise TwelveLabsServiceError(f"Network error during {what}: {exc}") from exc
                raise
            delay = _retry_after(exc) or base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log.warning("%s failed (%s); retry %d/%d in %.1fs", what, exc, attempt, attempts - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass
class AnswerPart:
    """One `output_text` content part. Annotation offsets are relative to this part's text."""

    text: str
    annotations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class JockeyAnswer:
    response_id: str | None
    session_id: str | None
    status: str | None
    parts: list[AnswerPart]
    incomplete_reason: str | None = None
    billing: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.parts).strip()


@dataclass
class SearchClip:
    item_id: str
    rank: int
    start_sec: float
    end_sec: float
    transcription: str | None = None


ANNOTATION_FIELDS = ("type", "start_index", "end_index", "item_id", "collection_id",
                     "start_sec", "end_sec", "title", "url", "thumbnail_url", "hls_url")


def _annotation_to_dict(ann: Any) -> dict[str, Any]:
    return {k: _get(ann, k) for k in ANNOTATION_FIELDS if _get(ann, k) is not None}


def parse_response(resp: Any) -> JockeyAnswer:
    parts: list[AnswerPart] = []
    for item in _get(resp, "output") or []:
        if _get(item, "type") != "message":
            continue
        # Intermediate messages carry phase == "commentary"; no phase means final answer.
        if _get(item, "phase") not in (None, "final_answer"):
            continue
        for part in _get(item, "content") or []:
            if _get(part, "type") != "output_text":
                continue
            parts.append(AnswerPart(
                text=_get(part, "text") or "",
                annotations=[_annotation_to_dict(a) for a in (_get(part, "annotations") or [])],
            ))
    incomplete = _get(resp, "incomplete_details")
    billing = _get(resp, "billing")
    if billing is not None and not isinstance(billing, dict):
        billing = {"amount": _get(billing, "amount"), "currency": _get(billing, "currency")}
    return JockeyAnswer(
        response_id=_get(resp, "id"),
        session_id=_get(resp, "session_id"),
        status=str(_get(resp, "status")) if _get(resp, "status") is not None else None,
        parts=parts,
        incomplete_reason=_get(incomplete, "reason") if incomplete else None,
        billing=billing,
    )


class TwelveLabsService:
    def __init__(self, api_key: str, request_timeout: int = 600):
        if not api_key:
            raise TwelveLabsServiceError("TWELVELABS_API_KEY is not set. Add it to your .env file.")
        self.client = TwelveLabs(api_key=api_key)
        self.request_timeout = request_timeout

    def _opts(self, timeout: int | None = None) -> dict[str, Any]:
        # Retries are handled by `with_retry`; disable the SDK's own so we never double-retry.
        return {"timeout_in_seconds": timeout or self.request_timeout, "max_retries": 0}

    # Knowledge store -------------------------------------------------------
    def retrieve_knowledge_store(self, store_id: str) -> Any:
        return with_retry(lambda: self.client.knowledge_stores.retrieve(store_id, request_options=self._opts(60)),
                          what="retrieve knowledge store")

    def create_knowledge_store(self, name: str) -> str:
        store = with_retry(
            lambda: self.client.knowledge_stores.create(
                name=name,
                description="Videos uploaded through the Streamlit Video RAG app.",
                request_options=self._opts(60),
            ),
            attempts=2, what="create knowledge store",
        )
        log.info("Created knowledge store %s", store.id)
        return store.id

    # Assets ----------------------------------------------------------------
    def upload_video(self, path: Path, filename: str,
                     on_progress: Callable[[float, str], None] | None = None) -> str:
        """Upload a local video and return the new asset id."""
        size = path.stat().st_size
        if size > MULTIPART_UPLOAD_LIMIT:
            raise TwelveLabsServiceError("The video is larger than the 10 GB upload limit.")

        if size <= DIRECT_UPLOAD_LIMIT:
            def direct() -> Any:
                with open(path, "rb") as fh:
                    return self.client.assets.create(
                        method="direct", file=(filename, fh), filename=filename,
                        request_options=self._opts(max(self.request_timeout, 1800)),
                    )
            if on_progress:
                on_progress(0.0, "Uploading (direct upload)…")
            asset = with_retry(direct, attempts=3, what="direct upload")
            if on_progress:
                on_progress(1.0, "Upload complete")
            return asset.id

        def progress(p: Any) -> None:
            if on_progress:
                on_progress(min(p.percentage / 100.0, 1.0),
                            f"Uploading (multipart) {p.completed_chunks}/{p.total_chunks} chunks")

        # The SDK's multipart helper retries individual chunks itself.
        result = with_retry(
            lambda: self.client.multipart_upload.upload_file(
                path, filename=filename, file_type="video", progress_callback=progress,
            ),
            attempts=2, what="multipart upload",
        )
        return result.asset_id

    def retrieve_asset(self, asset_id: str) -> Any:
        return with_retry(lambda: self.client.assets.retrieve(asset_id, request_options=self._opts(60)),
                          what="retrieve asset")

    def wait_for_asset(self, asset_id: str, timeout: int,
                       on_status: Callable[[str, int], None] | None = None, interval: int = 5) -> Any:
        start = time.monotonic()
        while True:
            asset = self.retrieve_asset(asset_id)
            status = str(asset.status)
            elapsed = int(time.monotonic() - start)
            if on_status:
                on_status(status, elapsed)
            if status == "ready":
                return asset
            if status == "failed":
                err = _get(_get(asset, "error"), "message")
                raise TwelveLabsServiceError(f"TwelveLabs could not process the video asset. {err or ''}".strip())
            if elapsed > timeout:
                raise TwelveLabsServiceError("Timed out waiting for the asset to become ready. "
                                             "Use 'Resume processing' to keep waiting.")
            time.sleep(interval)

    # Knowledge store items -------------------------------------------------
    def add_item(self, store_id: str, asset_id: str, metadata: dict[str, Any] | None = None) -> Any:
        return with_retry(
            lambda: self.client.knowledge_store_items.create(
                store_id, asset_id=asset_id, asset_type="video",
                **({"metadata": metadata} if metadata else {}),
                request_options=self._opts(60),
            ),
            attempts=3, what="add knowledge store item",
        )

    def retrieve_item(self, store_id: str, item_id: str) -> Any:
        return with_retry(
            lambda: self.client.knowledge_store_items.retrieve(store_id, item_id, request_options=self._opts(60)),
            what="retrieve knowledge store item",
        )

    def wait_for_item(self, store_id: str, item_id: str, timeout: int,
                      on_status: Callable[[str, int], None] | None = None, interval: int = 10) -> Any:
        start = time.monotonic()
        while True:
            item = self.retrieve_item(store_id, item_id)
            status = str(item.status)
            elapsed = int(time.monotonic() - start)
            if on_status:
                on_status(status, elapsed)
            if status == "ready":
                return item
            if status == "failed":
                raise TwelveLabsServiceError("Indexing failed for this video in the knowledge store.")
            if elapsed > timeout:
                raise TwelveLabsServiceError("Timed out waiting for indexing. "
                                             "Use 'Resume processing' to keep waiting.")
            time.sleep(interval)

    def delete_item(self, store_id: str, item_id: str) -> None:
        with_retry(lambda: self.client.knowledge_store_items.delete(store_id, item_id, request_options=self._opts(60)),
                   what="delete knowledge store item")

    # Jockey responses ------------------------------------------------------
    def ask(self, store_id: str, item_id: str, question: str, session_id: str | None = None,
            on_text: Callable[[str], None] | None = None) -> JockeyAnswer:
        """Ask Jockey about one knowledge store item and stream the answer text.

        The question is scoped to the video with `selections` + a `{{sel:0}}` token.
        """
        content = "About the video {{sel:0}}: " + question.strip()
        kwargs: dict[str, Any] = {
            "knowledge_store_id": store_id,
            "input": [{"type": "message", "role": "user", "content": content[:10000]}],
            "instructions": JOCKEY_INSTRUCTIONS,
            "selections": [{"kind": "item", "id": item_id}],
            "request_options": self._opts(),
        }
        if session_id:
            kwargs["session_id"] = session_id

        def run() -> JockeyAnswer:
            streamed = ""
            final = None
            for event in self.client.responses.create_stream(**kwargs):
                etype = _get(event, "type")
                if etype == "response.output_text.delta":
                    streamed += _get(event, "delta") or ""
                    if on_text:
                        on_text(streamed)
                elif etype in ("response.completed", "response.failed"):
                    final = _get(event, "response")
            if final is None:
                raise TwelveLabsServiceError("The Jockey stream ended without a final response.")
            answer = parse_response(final)
            if answer.status == "failed":
                raise TwelveLabsServiceError("Jockey could not generate a response for this question.")
            return answer

        return with_retry(run, attempts=3, what="Jockey response")

    # Direct search (fallback for exact clip boundaries) -------------------
    def search_clips(self, store_id: str, item_id: str, query: str, limit: int = 5) -> list[SearchClip]:
        result = with_retry(
            lambda: self.client.knowledge_stores.search(
                store_id,
                query={"text": query[:500]},
                filter={"item_id": {"eq": item_id}},
                search_options={"video": {"modalities": ["visual", "audio"]}},
                page_size=limit,
                request_options=self._opts(120),
            ),
            what="knowledge store search",
        )
        clips: list[SearchClip] = []
        for hit in _get(result, "data") or []:
            for match in _get(hit, "matches") or []:
                clips.append(SearchClip(
                    item_id=_get(hit, "item_id"),
                    rank=_get(hit, "rank") or len(clips) + 1,
                    start_sec=float(_get(match, "start_sec")),
                    end_sec=float(_get(match, "end_sec")),
                    transcription=_get(match, "transcription"),
                ))
        return clips[:limit]
