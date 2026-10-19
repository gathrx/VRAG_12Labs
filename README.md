# Video RAG with TwelveLabs Jockey

A Streamlit app for asking natural-language questions about your own videos. You upload a video, TwelveLabs indexes it into a **knowledge store**, and **Jockey** (the TwelveLabs Agents API) finds the moments that answer each question. Every result shows the real timestamp range, a frame extracted from your original file, a short description of the scene, and a button that plays the video from that moment.

> Example: *"Where is the red car?"* returns a card with the frame from the cited range (e.g. `00:34–00:41`), what happens in that scene, and **▶ Play from 00:34**.

## How it works

```
Upload (Streamlit)
  └─ assets.create (direct ≤200 MB, multipart ≤10 GB) ─ poll assets.retrieve → ready
       └─ knowledge_store_items.create (into one app knowledge store) ─ poll → ready
            └─ responses.create_stream  (Jockey, scoped to the video with selections + {{sel:0}})
                 ├─ answer text streams into the chat
                 └─ video_citation annotations → item_id, start_sec, end_sec
                      ├─ validated against the local file's duration
                      ├─ fallback: knowledge_stores.search filtered to the item (only if Jockey
                      │            answered but gave no time range)
                      └─ OpenCV (FFmpeg fallback) extracts the frame at the moment's midpoint
```

**Where the timestamps come from.** Jockey grounds its answers with citation markers such as `[1]`. Each marker has a `video_citation` annotation that carries `item_id`, `start_sec`, and `end_sec`. The app reads those annotations directly and does not parse timestamps out of the generated text. Plain-text responses are used deliberately: the docs say *"Structured responses contain no citations"*, so JSON-schema output would lose these API-verified ranges.

**No match.** Jockey is told to reply `NO_MATCH: …` when nothing relevant is in the video. The app then shows a clear "No relevant moment found" message and no cards.

**Persistence.** Video records, upload/index progress, TwelveLabs IDs, Jockey session IDs (for follow-up questions), and chat history live in `data/metadata.json`. Videos and frames live in `data/videos` and `data/frames`. If you close the app during indexing, select the video later and click **Resume processing**.

## Project layout

| File | Purpose |
| --- | --- |
| `app.py` | Streamlit UI: library, upload, processing status, player, chat, result cards |
| `vrag/config.py` | Loads `.env` settings (server-side only) and sets up logging to `data/app.log` |
| `vrag/twelvelabs_service.py` | SDK wrapper: upload, polling, knowledge store, Jockey streaming, search, retry/backoff, error messages |
| `vrag/pipeline.py` | Resumable upload → index workflow, and the question → moments → frames flow |
| `vrag/moments.py` | Turns `video_citation` annotations into timestamped moments with descriptions |
| `vrag/frames.py` | Reads the video duration and extracts real frames (OpenCV, FFmpeg fallback) |
| `vrag/storage.py` | Thread-safe JSON metadata store with atomic writes |
| `scripts/smoke_test.py` | End-to-end test against the live API |
| `tests/` | Unit tests for citation parsing |

## Windows installation

Requirements: **Python 3.10+** (tested on 3.11), and optionally **FFmpeg** on `PATH`. FFmpeg converts AVI/MKV files for in-browser playback and acts as a frame-extraction fallback.

```powershell
cd C:\Users\Admin\OneDrive\Desktop\VRAG_12Labs

# 1. Create and activate a virtual environment
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
#   If PowerShell blocks the script:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#   (cmd.exe users: .venv\Scripts\activate.bat)

# 2. Install dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. Configure your API key
copy .env.example .env
notepad .env        # set TWELVELABS_API_KEY=tlk_...

# 4. Run
streamlit run app.py
```

The app opens at http://localhost:8501.

Optional FFmpeg install: `winget install Gyan.FFmpeg`

## Using the app

1. In the sidebar, open **➕ Add a video**, then either:
   - **📁 Upload file**: choose a file and click **Upload & index**, or
   - **▶️ YouTube link**: paste a `youtube.com/watch`, `youtu.be`, or `youtube.com/shorts` link and click **Download & index**.

   Progress appears for each step (download, upload, asset processing, knowledge-store indexing). Indexing can take several minutes depending on the video's length.

   TwelveLabs URL uploads don't accept video-hosting sites like YouTube, and frames have to be extracted from a local file. So YouTube links are downloaded first with `yt-dlp`, up to 1080p and 2 GB. Merging the separate video and audio streams needs FFmpeg; without FFmpeg, a lower-quality single-file stream is used. `yt-dlp` uses Node.js or Deno, if installed, to solve YouTube's JavaScript challenges. Only download videos you have the right to use. Private, age-restricted, and live videos can't be downloaded.
2. When the badge shows **Ready**, ask a question in the chat.
3. Each result card shows the extracted frame, the timestamp range, the source (Jockey citation or knowledge-store search), and a description. Click **▶ Play from mm:ss** to jump the player to that moment.
4. Follow-up questions continue the same Jockey session. **🧹 New conversation** starts a fresh one.

## Configuration (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TWELVELABS_API_KEY` | *(required)* | Your API key. Read only on the server; never sent to the browser |
| `TWELVELABS_KNOWLEDGE_STORE_ID` | empty | Reuse an existing store. If empty, the app creates `vrag-streamlit` on the first upload and remembers it |
| `TWELVELABS_KNOWLEDGE_STORE_NAME` | `vrag-streamlit` | Name used when the app creates a store |
| `TWELVELABS_REQUEST_TIMEOUT` | `600` | Seconds per API request (Jockey answers can take a few minutes) |
| `TWELVELABS_INDEX_TIMEOUT` | `7200` | Seconds to wait for processing or indexing before asking you to resume |
| `VRAG_DATA_DIR` | `data` | Location for videos, frames, metadata, and logs |

## Testing

```powershell
pip install pytest
python -m pytest -q tests                                      # offline unit tests
python scripts\smoke_test.py --check-key                        # verifies key + knowledge store access
python scripts\smoke_test.py C:\path\to\video.mp4 "Where is the red car?"   # full live run (uses credits)
```

## Error handling and retries

- HTTP 429 and 5xx responses and network errors are retried with exponential backoff and jitter, and `Retry-After` is honoured. Client errors (400/401/403/404) are not retried, as the TwelveLabs error-handling guide recommends.
- Errors are turned into readable messages: invalid key, missing permission, rate limit, and so on.
- An expired Jockey session (HTTP 400) is retried once as a new session.
- If an asset or index item fails, the app clears it so **Retry processing** starts that step over.

## Things to verify for your account

- **Agents (Jockey) access.** Knowledge stores and `/responses` belong to the Agents API. If your plan doesn't include them you'll see an HTTP 403 message. Check the [Playground](https://playground.twelvelabs.io/) or your plan.
- **Billing.** Indexing, Jockey responses, and search count toward your TwelveLabs usage. Each answer shows Jockey's reported list-price cost when the API returns one.
- **Citation granularity.** Jockey decides how it cites. If it cites the whole video without a range, the app uses the documented knowledge-store search API, filtered to that video, to find exact clips, and labels those cards as search results.
- **SDK version.** This app was built against `twelvelabs==1.3.6` and the v1.3 docs. In that version the SDK's `ResponseObject` model doesn't declare `billing`, so the app reads it defensively.
- **Upload sizes.** Streamlit holds uploads in memory, and `.streamlit/config.toml` caps them at 2 GB. TwelveLabs accepts up to 10 GB through multipart upload.
- **Delete.** Deleting a video can also remove its knowledge-store item. The underlying asset stays in your TwelveLabs account.

## Docs used

- Agents quickstart: https://docs.twelvelabs.io/v1.3/agents/get-started/quickstart/create-a-response
- Create a response / citations: https://docs.twelvelabs.io/v1.3/api-reference/responses/create
- Agentic search recipe: https://docs.twelvelabs.io/v1.3/agents/recipes/agentic-search
- Search a knowledge store: https://docs.twelvelabs.io/v1.3/agents/guides/search-a-knowledge-store
- Upload content: https://docs.twelvelabs.io/v1.3/agents/guides/upload-content
- Error handling: https://docs.twelvelabs.io/v1.3/agents/troubleshooting/error-handling
