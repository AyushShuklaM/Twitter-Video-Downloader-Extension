"""
Multi-platform video downloader — backend API

Supports: Twitter/X, YouTube, Instagram, Reddit, Pinterest, Snapchat, ShareChat.
(Not supported: Messenger — its videos live inside private conversations with
no public URL, so there's nothing a link-based tool can reach.)

Endpoints:
  GET  /api/health
  POST /api/info        { "url": "<post url>" }  -> metadata + available qualities
  GET  /api/download     ?url=<post url>&format=mp4|mp3&quality=<height or 'best'>
                          -> streams the converted file back to the client

Requires:
  pip install -r requirements.txt
  ffmpeg installed and on PATH (needed for mp3 extraction and muxing)

Run:
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from starlette.background import BackgroundTask

app = FastAPI(title="Multi-Platform Video Downloader API")

# Allow the extension + the web frontend to call this API from any origin.
# Tighten this to your real frontend/extension origin(s) before deploying publicly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# Domains we accept. Messenger is intentionally excluded — see module docstring.
SUPPORTED_DOMAINS = (
    r"(twitter\.com|x\.com|"
    r"youtube\.com|youtu\.be|"
    r"instagram\.com|"
    r"reddit\.com|redd\.it|"
    r"pinterest\.[a-z.]+|pin\.it|"
    r"snapchat\.com|"
    r"sharechat\.com)"
)
SUPPORTED_URL_RE = re.compile(
    rf"^https?://([a-z0-9-]+\.)?{SUPPORTED_DOMAINS}/\S+", re.IGNORECASE
)

TMP_ROOT = Path(tempfile.gettempdir()) / "twdl"
TMP_ROOT.mkdir(exist_ok=True)


class InfoRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not SUPPORTED_URL_RE.match(v):
            raise ValueError(
                "That doesn't look like a supported link. Supported: "
                "Twitter/X, YouTube, Instagram, Reddit, Pinterest, Snapchat, ShareChat."
            )
        return v


def _base_ydl_opts(workdir: Path) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        # Speed: fetch multiple fragments of a video in parallel instead of one-by-one
        "concurrent_fragment_downloads": 8,
        # Speed: use aria2c (multi-connection downloader) instead of yt-dlp's built-in
        # single-connection downloader, when available. Falls back silently if aria2c
        # isn't installed (see Dockerfile).
        "external_downloader": "aria2c",
        "external_downloader_args": {
            "aria2c": ["-x", "8", "-s", "8", "-k", "1M"]
        },
        # Twitter frequently needs a normal-looking UA to serve video variants.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/info")
def get_info(payload: InfoRequest):
    """Look up a post/video and return its available qualities without downloading."""
    with tempfile.TemporaryDirectory(dir=TMP_ROOT) as tmp:
        opts = _base_ydl_opts(Path(tmp))
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(payload.url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(
                status_code=422,
                detail="Couldn't find a downloadable video there. "
                       "It may not contain video, or the post may be private/deleted.",
            ) from e

        formats = info.get("formats") or []
        qualities = []
        seen_heights = set()
        for f in formats:
            height = f.get("height")
            if f.get("vcodec") not in (None, "none") and height and height not in seen_heights:
                seen_heights.add(height)
                qualities.append(height)
        qualities.sort(reverse=True)

        return {
            "id": info.get("id"),
            "title": info.get("title") or info.get("description", "")[:120],
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "qualities": qualities or ["best"],
        }


@app.get("/api/download")
def download(
    url: str = Query(...),
    format: str = Query("mp4", pattern="^(mp4|mp3)$"),
    quality: Optional[str] = Query("best"),
):
    if not SUPPORTED_URL_RE.match(url.strip()):
        raise HTTPException(status_code=400, detail="Invalid or unsupported URL.")

    job_dir = TMP_ROOT / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=True)

    opts = _base_ydl_opts(job_dir)

    if format == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        if quality and quality != "best":
            opts["format"] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if format == "mp3":
                filename = str(Path(filename).with_suffix(".mp3"))
            elif not filename.endswith(".mp4"):
                filename = str(Path(filename).with_suffix(".mp4"))
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=422,
            detail="Couldn't download that video. It may not exist, be private, or contain no video.",
        ) from e

    if not os.path.exists(filename):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Conversion failed unexpectedly.")

    title = (info.get("title") or info.get("id") or "video").strip()
    # Collapse whitespace, strip anything that isn't filename-safe, and cap the length.
    safe_title = re.sub(r"\s+", " ", title)
    safe_title = re.sub(r"[^\w\- ]", "", safe_title).strip()
    safe_title = safe_title[:30].strip() or "video"
    safe_title = safe_title.replace(" ", "_")
    download_name = f"{safe_title}.{format}"

    return FileResponse(
        path=filename,
        filename=download_name,
        media_type="video/mp4" if format == "mp4" else "audio/mpeg",
        background=BackgroundTask(shutil.rmtree, job_dir, ignore_errors=True),
    )