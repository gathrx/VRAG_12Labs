"""End-to-end check against the real TwelveLabs API (uses your .env key; incurs normal usage).

Usage (from the project folder, venv active):
    python scripts/smoke_test.py path\\to\\video.mp4 "Where is the red car?"
    python scripts/smoke_test.py --check-key
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vrag import storage as S  # noqa: E402
from vrag.config import configure_logging, load_settings  # noqa: E402
from vrag.frames import probe_video  # noqa: E402
from vrag.moments import format_range  # noqa: E402
from vrag.pipeline import answer_question, ensure_knowledge_store, process_video  # noqa: E402
from vrag.twelvelabs_service import TwelveLabsService, TwelveLabsServiceError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", nargs="?")
    parser.add_argument("question", nargs="?", default="Summarize the key scenes with timestamps")
    parser.add_argument("--check-key", action="store_true", help="only verify the key and knowledge store access")
    args = parser.parse_args()

    settings = load_settings()
    configure_logging(settings)
    store = S.MetadataStore(settings.metadata_file)
    try:
        service = TwelveLabsService(settings.api_key, settings.request_timeout)
        ks = ensure_knowledge_store(service, store, settings)
        print(f"OK  knowledge store: {ks}")
        if args.check_key:
            return 0
        if not args.video:
            parser.error("video path required unless --check-key")

        path = Path(args.video).resolve()
        dest = settings.videos_dir / path.name
        if not dest.exists():
            dest.write_bytes(path.read_bytes())
        info = probe_video(dest)
        rec = store.create_video(filename=path.name, local_path=str(dest), size_bytes=dest.stat().st_size,
                                 duration_sec=info.duration_sec, width=info.width, height=info.height)
        process_video(rec["id"], service, store, settings, lambda m, p: print(f"... {m}"))
        print("OK  video indexed")

        msg = answer_question(store.get_video(rec["id"]), args.question, service, store, settings,
                              on_step=lambda m: print(f"... {m}"))
        print("\nANSWER:\n" + msg["content"])
        for m in msg["moments"]:
            print(f"  {format_range(m['start_sec'], m['end_sec'])}  [{m['source']}]  frame={m['frame_path']}")
            print(f"     {m['description']}")
        if not msg["moments"]:
            print("  (no matching moments)")
        return 0
    except TwelveLabsServiceError as exc:
        print(f"FAIL {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
