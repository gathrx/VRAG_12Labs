"""Resumable upload -> asset -> knowledge store item workflow, and the question-answering flow."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from . import storage as S
from .config import Settings
from .frames import extract_frame, representative_time
from .moments import (clean_no_match, is_no_match, moments_from_citations, moments_from_search,
                      render_answer)
from .twelvelabs_service import TwelveLabsService, TwelveLabsServiceError

log = logging.getLogger("vrag.pipeline")

Reporter = Callable[[str, float | None], None]  # (message, progress 0..1 or None)

ITEM_STATUS_TEXT = {
    "queued": "Queued for indexing",
    "pending": "Waiting for an indexing worker",
    "processing": "Jockey is indexing the video (scenes, entities, speech, embeddings)",
    "ready": "Indexed",
}


def ensure_knowledge_store(service: TwelveLabsService, store: S.MetadataStore, settings: Settings) -> str:
    """Use the configured/persisted knowledge store, creating one on first run."""
    store_id = settings.knowledge_store_id or store.get_knowledge_store_id()
    if store_id:
        try:
            service.retrieve_knowledge_store(store_id)
            if store.get_knowledge_store_id() != store_id:
                store.set_knowledge_store_id(store_id)
            return store_id
        except TwelveLabsServiceError as exc:
            if exc.status_code != 404 or settings.knowledge_store_id:
                raise
            log.warning("Persisted knowledge store %s no longer exists; creating a new one", store_id)
    store_id = service.create_knowledge_store(settings.knowledge_store_name)
    store.set_knowledge_store_id(store_id)
    return store_id


def process_video(video_id: str, service: TwelveLabsService, store: S.MetadataStore,
                  settings: Settings, report: Reporter) -> dict[str, Any]:
    """Run (or resume) every step until the video is ready to query. Safe to call repeatedly."""
    rec = store.get_video(video_id)
    if rec is None:
        raise TwelveLabsServiceError("Video record not found.")
    path = Path(rec["local_path"])
    if not path.exists():
        raise TwelveLabsServiceError(f"The local video file is missing: {path}")

    report("Connecting to your knowledge store…", None)
    ks_id = ensure_knowledge_store(service, store, settings)
    if rec.get("knowledge_store_id") and rec["knowledge_store_id"] != ks_id:
        # The knowledge store changed (e.g. .env edited); the item must be re-added.
        rec = store.update_video(video_id, item_id=None, session_id=None)

    stage = "upload"
    try:
        if not rec.get("asset_id"):
            store.update_video(video_id, status=S.STATUS_UPLOADING, error=None)
            asset_id = service.upload_video(path, rec["filename"], on_progress=lambda p, m: report(m, p))
            rec = store.update_video(video_id, asset_id=asset_id, status=S.STATUS_ASSET_PROCESSING)

        stage = "asset"
        if not rec.get("item_id"):
            store.update_video(video_id, status=S.STATUS_ASSET_PROCESSING, error=None)
            service.wait_for_asset(
                rec["asset_id"], settings.index_timeout,
                on_status=lambda s, t: report(f"Asset status: {s} ({t}s)", None),
            )
            report("Adding the video to the knowledge store…", None)
            item = service.add_item(ks_id, rec["asset_id"])
            rec = store.update_video(video_id, item_id=item.id, knowledge_store_id=ks_id,
                                     status=S.STATUS_INDEXING)

        stage = "item"
        store.update_video(video_id, status=S.STATUS_INDEXING, error=None)
        item = service.wait_for_item(
            ks_id, rec["item_id"], settings.index_timeout,
            on_status=lambda s, t: report(f"{ITEM_STATUS_TEXT.get(s, s)} ({t}s elapsed)", None),
        )
        system = getattr(item, "system_metadata", None)
        api_duration = getattr(system, "duration", None) if system is not None else None
        return store.update_video(video_id, status=S.STATUS_READY, error=None,
                                  api_duration_sec=api_duration, indexed_at=S.utc_now())
    except TwelveLabsServiceError as exc:
        fields: dict[str, Any] = {"status": S.STATUS_FAILED, "error": str(exc)}
        # A failed asset/item cannot recover; clear it so "Resume" starts that step over.
        if stage == "asset" and "could not process" in str(exc):
            fields["asset_id"] = None
        if stage == "item" and "Indexing failed" in str(exc):
            fields["item_id"] = None
        store.update_video(video_id, **fields)
        raise


def frame_path_for(settings: Settings, video_id: str, start: float, end: float) -> Path:
    return settings.frames_dir / video_id / f"{int(start * 1000)}_{int(end * 1000)}.jpg"


def answer_question(video: dict[str, Any], question: str, service: TwelveLabsService,
                    store: S.MetadataStore, settings: Settings,
                    on_text: Callable[[str], None] | None = None,
                    on_step: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Ask Jockey, turn its citations into moments with real frames, and persist the chat turn."""
    step = on_step or (lambda _m: None)
    ks_id, item_id = video["knowledge_store_id"], video["item_id"]
    duration = video.get("duration_sec") or video.get("api_duration_sec")

    step("Jockey is searching and reasoning over the video…")
    try:
        answer = service.ask(ks_id, item_id, question, session_id=video.get("session_id"), on_text=on_text)
    except TwelveLabsServiceError as exc:
        if video.get("session_id") and exc.status_code == 400:
            # Session expired or invalid: start a fresh conversation once.
            log.info("Session rejected; retrying without session_id")
            answer = service.ask(ks_id, item_id, question, session_id=None, on_text=on_text)
        else:
            raise
    if answer.session_id:
        store.update_video(video["id"], session_id=answer.session_id)

    moments, cited_whole = moments_from_citations(answer.parts, item_id, duration)
    raw_text = answer.text
    no_match = is_no_match(raw_text) and not moments

    fallback = False
    if not moments and not no_match and raw_text:
        # Jockey answered but gave no usable time range: locate clips with the direct search API.
        step("Locating exact clips with knowledge store search…")
        try:
            clips = service.search_clips(ks_id, item_id, question, limit=4 if cited_whole else 3)
            moments = moments_from_search(clips, duration, question)
            fallback = bool(moments)
        except TwelveLabsServiceError as exc:
            log.warning("Search fallback failed: %s", exc)

    step("Extracting frames from the original video…")
    video_path = Path(video["local_path"])
    moment_dicts = []
    for m in moments:
        t = representative_time(m.start_sec, m.end_sec, duration)
        frame = extract_frame(video_path, t, frame_path_for(settings, video["id"], m.start_sec, m.end_sec))
        d = m.to_dict()
        d["frame_time_sec"] = t
        d["frame_path"] = str(frame.relative_to(settings.data_dir)) if frame else None
        moment_dicts.append(d)

    content = clean_no_match(raw_text) if no_match else render_answer(answer.parts, item_id)
    message = {
        "role": "assistant",
        "content": content or "Jockey returned an empty answer.",
        "moments": moment_dicts,
        "no_match": no_match or not moment_dicts,
        "fallback_search": fallback,
        "response_id": answer.response_id,
        "status": answer.status,
        "incomplete_reason": answer.incomplete_reason,
        "billing": answer.billing,
        "created_at": S.utc_now(),
    }
    store.append_chat(video["id"], message)
    return message
