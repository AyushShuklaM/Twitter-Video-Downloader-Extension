# Snagpost — Twitter/X video downloader

Three pieces that work together:

```
backend/     FastAPI server that resolves tweet URLs and converts to mp4/mp3 (uses yt-dlp + ffmpeg)
frontend/    A single-page site: paste a link, get a download button
extension/   Manifest V3 browser extension: detects the post you're viewing, one-click download
```

The extension and the website both just call the backend's API. You need the backend
running somewhere reachable before either of the other two will work.

## 1. Run the backend

Requirements: Python 3.9+, and **ffmpeg** installed and on your PATH (needed for
audio extraction and muxing separate video/audio streams into one mp4).

```bash
cd backend
python3 -m venv venv && source venv/bin/activate     # optional but recommended
pip install -r requirements.txt

# macOS:   brew install ffmpeg
# Ubuntu:  sudo apt install ffmpeg
# Windows: winget install ffmpeg

uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Check it's alive: open `http://localhost:8000/api/health` — should return `{"status":"ok"}`.

**Note on reliability:** this uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to resolve
video URLs, since Twitter/X's video delivery changes often and yt-dlp is actively
maintained against it. Keep it updated (`pip install -U yt-dlp`) if downloads start failing —
that's almost always the first fix to try.

## 2. Run the website

`frontend/index.html` is a static file — no build step. Two options:

- Just open it directly in a browser (double-click it), or
- Serve it so relative paths behave the same as production, e.g. `python3 -m http.server 5500`
  from inside `frontend/`, then visit `http://localhost:5500`.

At the top of the `<script>` block, `API_BASE` is set to `http://localhost:8000`.
Change it to wherever you deploy the backend (see "Deploying" below).

## 3. Load the extension

1. In Chrome/Edge/Brave, go to `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**, and select the `extension/` folder
4. Pin the extension, open any individual X/Twitter post (a URL containing `/status/…`), click the icon

At the top of `popup.js`, `API_BASE` and `FRONTEND_URL` point at localhost by default —
update both once you deploy.

## How it works

- `POST /api/info` — given a tweet URL, returns title/thumbnail/available video qualities
  without downloading anything (used to populate the quality dropdown).
- `GET /api/download` — given a tweet URL + format (`mp4`/`mp3`) + quality, downloads the
  video with yt-dlp, converts if needed with ffmpeg, and streams the file back.

Both the website and extension are thin clients around these two endpoints — all the
actual video resolution work happens server-side.

## Deploying beyond localhost

- Backend: any host that lets you run a long-lived Python process + ffmpeg
  (Render, Railway, Fly.io, a small VPS, etc). Put it behind HTTPS.
- Frontend: any static host (Netlify, Vercel, GitHub Pages, S3+CloudFront).
- Extension: update `API_BASE`/`FRONTEND_URL` in `popup.js`, then either keep loading it
  unpacked for personal use, or zip it and submit to the Chrome Web Store if you want
  others to install it (their listing policies do apply — see note below).

## Legal note

This is for personal use — saving clips from posts you have the right to save
(your own content, or things covered by fair use/personal archiving in your jurisdiction).
Downloading and redistributing other people's copyrighted video without permission
can violate copyright law and X's Terms of Service. If you plan to host the website or
publish the extension publicly for other people to use, it's worth reviewing X's terms
and applicable copyright law for your situation first.
