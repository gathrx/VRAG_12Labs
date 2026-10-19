"""Video RAG with TwelveLabs Jockey (Agents API) — Streamlit UI.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import html
import logging
import math
import mimetypes
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import streamlit as st

from vrag import storage as S
from vrag.config import configure_logging, load_settings
from vrag.frames import probe_video
from vrag.moments import SOURCE_SEARCH, format_range, format_ts
from vrag.pipeline import answer_question, process_video
from vrag.twelvelabs_service import TwelveLabsService, TwelveLabsServiceError
from vrag.ui_style import CSS
from vrag.youtube import YouTubeDownloadError, download_youtube, is_youtube_url

st.set_page_config(page_title="Video RAG · TwelveLabs Jockey", page_icon="🎬", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)

settings = load_settings()
configure_logging(settings)
log = logging.getLogger("vrag.app")

VIDEO_TYPES = ["mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv", "mpeg", "mpg", "3gp", "flv"]
BROWSER_PLAYABLE = {".mp4", ".m4v", ".webm", ".mov"}
MAX_UPLOAD_MB = 2048
SUGGESTIONS = [
    "Summarize the key scenes with timestamps",
    "When does a person first appear?",
    "Find every moment with on-screen text",
]
BUSY = {S.STATUS_SAVED, S.STATUS_UPLOADING, S.STATUS_ASSET_PROCESSING, S.STATUS_INDEXING}


@st.cache_resource
def get_store(path: str) -> S.MetadataStore:
    return S.MetadataStore(Path(path))


@st.cache_resource
def get_service(api_key: str, timeout: int) -> TwelveLabsService:
    return TwelveLabsService(api_key, timeout)


store = get_store(str(settings.metadata_file))


def service() -> TwelveLabsService:
    return get_service(settings.api_key, settings.request_timeout)


# ---------------------------------------------------------------- helpers
def badge(status: str) -> str:
    cls = {"ready": "vr-ready", "failed": "vr-failed"}.get(status, "vr-busy" if status in BUSY else "vr-idle")
    return f'<span class="vr-badge {cls}">{S.STATUS_LABELS.get(status, status)}</span>'


def status_icon(status: str) -> str:
    return {"ready": "✅", "failed": "⚠️"}.get(status, "⏳")


def human_size(n: int | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def set_play(video_id: str, start: float, end: float) -> None:
    st.session_state["play"] = {"video_id": video_id, "start": start, "end": end}


def make_playback_copy(src: Path) -> Path | None:
    """Browsers cannot play AVI/MKV/etc. Create an H.264 MP4 copy for the player (needs FFmpeg)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dst = src.with_name(src.stem + "_playback.mp4")
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-movflags", "+faststart", str(dst)],
            capture_output=True, timeout=3600, check=True,
        )
        return dst
    except subprocess.SubprocessError as exc:
        log.warning("Could not create playback copy: %s", exc)
        return None


def finalize_local_video(video_id: str, path: Path, **extra: Any) -> dict[str, Any]:
    """Record the local file's properties and make a browser-playable copy if needed."""
    info = probe_video(path)
    fields: dict[str, Any] = {
        "local_path": str(path), "size_bytes": path.stat().st_size, "duration_sec": info.duration_sec,
        "fps": info.fps, "width": info.width, "height": info.height, **extra,
    }
    if path.suffix.lower() not in BROWSER_PLAYABLE:
        with st.spinner("Converting to MP4 for in-browser playback…"):
            copy = make_playback_copy(path)
        fields["playback_path"] = str(copy) if copy else None
    return store.update_video(video_id, **fields)


def save_upload(uploaded) -> dict[str, Any]:
    name = Path(uploaded.name).name
    ext = Path(name).suffix.lower() or ".mp4"
    record = store.create_video(filename=name, local_path="", size_bytes=uploaded.size, source="upload")
    path = settings.videos_dir / f"{record['id']}{ext}"
    uploaded.seek(0)
    with open(path, "wb") as fh:
        shutil.copyfileobj(uploaded, fh, 8 * 1024 * 1024)
    return finalize_local_video(record["id"], path)


def save_youtube(url: str) -> dict[str, Any] | None:
    """Download a YouTube video into the library. Returns None (after showing an error) on failure."""
    record = store.create_video(filename="YouTube video", local_path="", size_bytes=0,
                                source="youtube", source_url=url.strip())
    with st.status("Downloading from YouTube…", expanded=True) as status:
        bar = st.progress(0.0)

        def report(progress: float | None, message: str) -> None:
            status.update(label=message)
            if progress is not None:
                bar.progress(min(max(progress, 0.0), 1.0))

        try:
            dl = download_youtube(url, settings.videos_dir, record["id"], on_progress=report)
        except YouTubeDownloadError as exc:
            store.delete_video(record["id"])
            for leftover in settings.videos_dir.glob(f"{record['id']}.*"):
                leftover.unlink(missing_ok=True)
            status.update(label="Download failed", state="error")
            st.error(str(exc))
            return None
        status.update(label=f"Downloaded “{dl.title}”", state="complete")
    title = re.sub(r'[<>:"/\\|?*]+', " ", dl.title).strip() or "YouTube video"
    return finalize_local_video(record["id"], dl.path, filename=f"{title}.mp4",
                                source_url=dl.webpage_url, youtube_id=dl.video_id)


def delete_video(video: dict[str, Any], remove_remote: bool) -> None:
    if remove_remote and video.get("item_id") and video.get("knowledge_store_id"):
        try:
            service().delete_item(video["knowledge_store_id"], video["item_id"])
        except TwelveLabsServiceError as exc:
            st.toast(f"Could not remove the item from TwelveLabs: {exc}", icon="⚠️")
    for key in ("local_path", "playback_path"):
        if video.get(key):
            Path(video[key]).unlink(missing_ok=True)
    shutil.rmtree(settings.frames_dir / video["id"], ignore_errors=True)
    store.delete_video(video["id"])
    st.session_state.pop("active_video_id", None)
    st.session_state.pop("play", None)


def run_processing(video_id: str) -> bool:
    with st.status("Processing video with TwelveLabs…", expanded=True) as status:
        bar = st.progress(0.0)
        line = st.empty()

        def report(message: str, progress: float | None) -> None:
            line.markdown(f"**{message}**")
            status.update(label=message)
            if progress is not None:
                bar.progress(min(max(progress, 0.0), 1.0))

        try:
            process_video(video_id, service(), store, settings, report)
        except TwelveLabsServiceError as exc:
            status.update(label="Processing stopped", state="error")
            st.error(str(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - keep the UI alive and record the failure
            log.exception("Unexpected processing error")
            store.update_video(video_id, status=S.STATUS_FAILED, error=f"Unexpected error: {exc}")
            status.update(label="Processing stopped", state="error")
            st.error(f"Unexpected error: {exc}")
            return False
        bar.progress(1.0)
        status.update(label="Video indexed and ready for questions", state="complete")
    return True


# ---------------------------------------------------------------- sidebar
def render_sidebar(videos: list[dict[str, Any]]) -> None:
    with st.sidebar:
        st.markdown("### 🎬 Video RAG")
        st.caption("Powered by TwelveLabs Jockey (Agents API)")

        with st.expander("➕ Add a video", expanded=not videos):
            render_uploader("sidebar_uploader")

        st.markdown("#### Library")
        if not videos:
            st.caption("No videos yet.")
        active = st.session_state.get("active_video_id")
        for v in videos:
            label = f"{status_icon(v['status'])}  {v['filename']}"
            if st.button(label, key=f"lib_{v['id']}", width="stretch",
                         type="primary" if v["id"] == active else "secondary"):
                st.session_state["active_video_id"] = v["id"]
                st.session_state.pop("play", None)
                st.rerun()

        st.divider()
        st.caption("Connection")
        st.markdown("API key: " + ("✅ configured (server-side)" if settings.api_key else "❌ missing"))
        ks = settings.knowledge_store_id or store.get_knowledge_store_id()
        st.markdown(f"Knowledge store: `{ks}`" if ks else "Knowledge store: created on first upload")


def start_processing(record: dict[str, Any]) -> None:
    st.session_state["active_video_id"] = record["id"]
    st.session_state["process_now"] = record["id"]
    st.session_state.pop("play", None)
    st.rerun()


def render_uploader(key: str) -> None:
    tab_file, tab_yt = st.tabs(["📁 Upload file", "▶️ YouTube link"])
    with tab_file:
        uploaded = st.file_uploader("Video file", type=VIDEO_TYPES, key=key, max_upload_size=MAX_UPLOAD_MB,
                                    help="Up to 200 MB uses direct upload; larger files (≤10 GB on TwelveLabs) "
                                         "use multipart upload.")
        if uploaded is not None and st.button("Upload & index", key=f"{key}_go", type="primary",
                                              width="stretch"):
            try:
                with st.spinner("Saving video…"):
                    record = save_upload(uploaded)
            except OSError as exc:
                st.error(f"Could not save the file: {exc}")
                return
            start_processing(record)
    with tab_yt:
        url = st.text_input("YouTube link", key=f"{key}_yt", placeholder="https://www.youtube.com/watch?v=…")
        if url and not is_youtube_url(url):
            st.caption("⚠️ Paste a youtube.com/watch, youtu.be or youtube.com/shorts link.")
        if st.button("Download & index", key=f"{key}_yt_go", type="primary", width="stretch",
                     disabled=not (url and is_youtube_url(url))):
            record = save_youtube(url)
            if record:
                start_processing(record)
        st.caption("The video is downloaded locally (≤1080p, ≤2 GB), then uploaded to TwelveLabs. "
                   "Only use videos you have the right to download.")


# ---------------------------------------------------------------- player
def render_player(video: dict[str, Any]) -> None:
    src = Path(video.get("playback_path") or video["local_path"])
    if not src.exists():
        st.error("The local video file is missing.")
        return
    play = st.session_state.get("play")
    start, autoplay = 0, False
    if play and play["video_id"] == video["id"]:
        start, autoplay = int(math.floor(play["start"])), True
    mime = mimetypes.guess_type(src.name)[0] or "video/mp4"
    if mime == "video/quicktime":
        mime = "video/mp4"  # most browsers play H.264 .mov files when labelled as mp4
    st.video(str(src), format=mime, start_time=start, autoplay=autoplay)
    if src.suffix.lower() not in BROWSER_PLAYABLE:
        st.caption("⚠️ This format may not play in the browser. Install FFmpeg to enable automatic conversion.")
    if play and play["video_id"] == video["id"]:
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.markdown(f'<span class="vr-ts">▶ {format_range(play["start"], play["end"])}</span>',
                    unsafe_allow_html=True)
        if c2.button("Reset", key="reset_play", width="stretch"):
            st.session_state.pop("play", None)
            st.rerun()

    dur = video.get("duration_sec") or video.get("api_duration_sec")
    res = f"{video['width']}×{video['height']}" if video.get("width") else "—"
    st.markdown(
        f'<div class="vr-meta">⏱ {format_ts(dur) if dur else "—"} &nbsp;·&nbsp; 🖥 {res} &nbsp;·&nbsp; '
        f'💾 {human_size(video.get("size_bytes"))}</div>', unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    if c1.button("🧹 New conversation", width="stretch", disabled=not video.get("chat")):
        store.update_video(video["id"], chat=[], session_id=None)
        st.session_state.pop("play", None)
        st.rerun()
    with c2.popover("🗑 Delete video", width="stretch"):
        st.write("Delete this video, its frames and its chat history?")
        remote = st.checkbox("Also remove it from the TwelveLabs knowledge store", value=True)
        if st.button("Delete permanently", type="primary"):
            delete_video(video, remote)
            st.rerun()
    with st.expander("Details"):
        st.markdown(
            (f"- Source: [{video['source_url']}]({video['source_url']})\n" if video.get("source_url") else "")
            + f"- Asset ID: `{video.get('asset_id') or '—'}`\n"
            f"- Knowledge store item: `{video.get('item_id') or '—'}`\n"
            f"- Jockey session: `{video.get('session_id') or 'new'}`\n"
            f"- Added: {video.get('created_at', '—')}"
        )


# ---------------------------------------------------------------- chat
def render_moment_card(video: dict[str, Any], msg_idx: int, j: int, m: dict[str, Any]) -> None:
    with st.container(border=True, key=f"card_{video['id']}_{msg_idx}_{j}"):
        frame = settings.data_dir / m["frame_path"] if m.get("frame_path") else None
        if frame and frame.exists():
            st.image(str(frame), width="stretch")
        else:
            st.info("Frame could not be extracted for this moment.")
        src = "Jockey citation" if m["source"] != SOURCE_SEARCH else "Knowledge-store search"
        st.markdown(
            f'<span class="vr-ts">{format_range(m["start_sec"], m["end_sec"])}</span>'
            f'<span class="vr-src">{src} · frame @ {format_ts(m.get("frame_time_sec", m["start_sec"]))}</span>'
            f'<div class="vr-desc">{html.escape(m.get("description") or "No description returned.")}</div>',
            unsafe_allow_html=True)
        if m.get("note"):
            st.markdown(f'<div class="vr-note">{html.escape(m["note"])}</div>', unsafe_allow_html=True)
        st.button(f"▶ Play from {format_ts(m['start_sec'])}", key=f"play_{video['id']}_{msg_idx}_{j}",
                  on_click=set_play, args=(video["id"], m["start_sec"], m["end_sec"]),
                  type="primary", width="stretch")


def render_message(video: dict[str, Any], idx: int, msg: dict[str, Any]) -> None:
    with st.chat_message(msg["role"], avatar="🧑" if msg["role"] == "user" else "🎬"):
        if msg["role"] == "user":
            st.markdown(msg["content"])
            return
        if msg.get("error"):
            st.error(msg["error"])
            return
        moments = msg.get("moments") or []
        if not moments:
            st.markdown(
                '<div class="vr-nomatch"><b>No relevant moment found in this video.</b><br>'
                f'{html.escape(msg.get("content") or "")}</div>', unsafe_allow_html=True)
        else:
            st.markdown(msg.get("content") or "")
        if msg.get("incomplete_reason"):
            st.warning(f"Jockey's answer was cut short ({msg['incomplete_reason']}). Try a narrower question.")
        if msg.get("fallback_search"):
            st.caption("ℹ️ Jockey did not return a time range, so these clips were located with "
                       "TwelveLabs knowledge-store search on this video.")
        if moments:
            st.caption(f"{len(moments)} matching moment{'s' if len(moments) != 1 else ''}")
            per_row = 2
            for row_start in range(0, len(moments), per_row):
                cols = st.columns(per_row)
                for j in range(row_start, min(row_start + per_row, len(moments))):
                    with cols[j - row_start]:
                        render_moment_card(video, idx, j, moments[j])
        billing = msg.get("billing") or {}
        if billing.get("amount"):
            st.caption(f"Jockey response cost (list price): {billing['amount']} {billing.get('currency', '')}")


def handle_question(video: dict[str, Any], question: str, chat_box) -> None:
    question = question.strip()
    if not question:
        return
    store.append_chat(video["id"], {"role": "user", "content": question, "created_at": S.utc_now()})
    with chat_box:
        with st.chat_message("user", avatar="🧑"):
            st.markdown(question)
        with st.chat_message("assistant", avatar="🎬"):
            status = st.status("Jockey is searching the video…", expanded=False)
            text_box = st.empty()
            try:
                answer_question(
                    video, question, service(), store, settings,
                    on_text=lambda t: text_box.markdown(t + " ▌"),
                    on_step=lambda m: status.update(label=m),
                )
                status.update(label="Done", state="complete")
            except TwelveLabsServiceError as exc:
                status.update(label="Request failed", state="error")
                store.append_chat(video["id"], {"role": "assistant", "error": str(exc), "created_at": S.utc_now()})
            except Exception as exc:  # noqa: BLE001
                log.exception("Unexpected error answering question")
                status.update(label="Request failed", state="error")
                store.append_chat(video["id"], {"role": "assistant", "error": f"Unexpected error: {exc}",
                                                "created_at": S.utc_now()})
    st.rerun()


def render_chat(video: dict[str, Any]) -> None:
    st.markdown("#### 💬 Ask about this video")
    chat_box = st.container(height=680, border=False)
    chat = video.get("chat") or []
    with chat_box:
        if not chat:
            st.caption("Ask a natural-language question. Jockey finds the matching moments and cites exact "
                       "timestamps; frames are pulled from your original file.")
            cols = st.columns(len(SUGGESTIONS))
            for i, s in enumerate(SUGGESTIONS):
                if cols[i].button(s, key=f"sugg_{i}", width="stretch"):
                    st.session_state["pending_question"] = s
                    st.rerun()
        for i, msg in enumerate(chat):
            render_message(video, i, msg)

    prompt = st.chat_input("e.g. Where is the red car?", key=f"chat_{video['id']}", max_chars=2000)
    pending = st.session_state.pop("pending_question", None)
    if prompt or pending:
        handle_question(video, prompt or pending, chat_box)


def render_processing_panel(video: dict[str, Any]) -> None:
    st.markdown("#### ⚙️ Processing")
    if st.session_state.get("process_now") == video["id"]:
        st.session_state.pop("process_now")
        if run_processing(video["id"]):
            st.rerun()
        return
    if video["status"] == S.STATUS_FAILED:
        st.error(video.get("error") or "Processing failed.")
        label = "🔁 Retry processing"
    else:
        st.info("This video is not ready yet. Processing continues on TwelveLabs even if you closed the page; "
                "resume to check its status and finish the remaining steps.")
        label = "▶ Resume processing"
    steps = [("Saved locally", True), ("Uploaded as asset", bool(video.get("asset_id"))),
             ("Added to knowledge store", bool(video.get("item_id"))), ("Indexed & ready", False)]
    st.markdown("\n".join(f"- {'✅' if done else '◻️'} {name}" for name, done in steps))
    if st.button(label, type="primary"):
        st.session_state["process_now"] = video["id"]
        st.rerun()


# ---------------------------------------------------------------- main
def main() -> None:
    videos = store.list_videos()
    render_sidebar(videos)

    if not settings.api_key:
        st.markdown('<div class="vr-hero"><h1>Set up your TwelveLabs API key</h1>'
                    '<p>The key stays on the server and is never sent to the browser.</p></div>',
                    unsafe_allow_html=True)
        st.markdown(
            "1. Create a key at **https://playground.twelvelabs.io/dashboard/api-keys**\n"
            "2. Copy `.env.example` to `.env` in the project folder\n"
            "3. Set `TWELVELABS_API_KEY=...` and restart: `streamlit run app.py`")
        st.stop()

    if not videos:
        st.markdown('<div class="vr-hero"><h1>Ask questions about any video</h1>'
                    '<p>Upload a video. TwelveLabs indexes it into a knowledge store, then Jockey finds the exact '
                    'moments that answer your questions — with real frames and timestamps.</p></div>',
                    unsafe_allow_html=True)
        render_uploader("main_uploader")
        return

    ids = {v["id"] for v in videos}
    if st.session_state.get("active_video_id") not in ids:
        st.session_state["active_video_id"] = videos[0]["id"]
    video = store.get_video(st.session_state["active_video_id"])

    st.markdown(f'<div class="vr-title"><h2>{html.escape(video["filename"])}</h2>{badge(video["status"])}</div>',
                unsafe_allow_html=True)

    left, right = st.columns([5, 7], gap="large")
    with left:
        with st.container(key="player_panel"):
            render_player(video)
    with right:
        if video["status"] == S.STATUS_READY:
            render_chat(video)
        else:
            render_processing_panel(video)


main()
