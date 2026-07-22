"""
Multi-platform video downloader — backend API

Supports: Twitter/X, YouTube, Instagram, Reddit, Pinterest, Snapchat, ShareChat.
(Not supported: Messenger — its videos live inside private conversations with
no public URL, so there's nothing a link-based tool can reach.)

Endpoints:
  GET  /api/health
  POST /api/info                     { "url": "<post url>" }  -> metadata + available qualities
  POST /api/download/start           ?url=&format=mp4|mp3&quality=  -> { job_id }
  GET  /api/download/progress/{id}   -> { status, percent, speed, eta }
  GET  /api/download/file/{id}       -> streams the finished file back to the client

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
import threading
import time
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

# In-memory job store: job_id -> progress/result info.
# Fine for a single-process deployment like this one; wouldn't survive a
# restart or scale across multiple worker processes.
JOBS: dict = {}
JOBS_LOCK = threading.Lock()


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
        "concurrent_fragment_downloads": 32,
        # Speed: use aria2c (multi-connection downloader) instead of yt-dlp's built-in
        # single-connection downloader, when available. Falls back silently if aria2c
        # isn't installed (see Dockerfile).
        "external_downloader": "aria2c",
        "external_downloader_args": {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "-j", "16"]
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


def _run_download_job(job_id: str, url: str, format: str, quality: Optional[str]):
    job_dir = TMP_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes") or 0
                percent = round(downloaded / total * 100, 1) if total else None
                job.update({
                    "status": "downloading",
                    "percent": percent,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                })
            elif d["status"] == "finished":
                job.update({"status": "processing", "percent": 95})

    def postprocessor_hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            if d["status"] == "started":
                job.update({"status": "processing", "percent": 97})
            elif d["status"] == "finished":
                job.update({"status": "processing", "percent": 99})

    opts = _base_ydl_opts(job_dir)
    opts["progress_hooks"] = [progress_hook]
    opts["postprocessor_hooks"] = [postprocessor_hook]

    if format == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
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

        if not os.path.exists(filename):
            raise RuntimeError("Conversion failed unexpectedly.")

        title = (info.get("title") or info.get("id") or "video").strip()
        safe_title = re.sub(r"\s+", " ", title)
        safe_title = re.sub(r"[^\w\- ]", "", safe_title).strip()
        safe_title = safe_title[:30].strip() or "video"
        safe_title = safe_title.replace(" ", "_")
        download_name = f"{safe_title}.{format}"

        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "finished",
                "percent": 100,
                "filepath": filename,
                "download_name": download_name,
                "job_dir": str(job_dir),
            })
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "error": str(e)})
        shutil.rmtree(job_dir, ignore_errors=True)


@app.post("/api/download/start")
def start_download(
    url: str = Query(...),
    format: str = Query("mp4", pattern="^(mp4|mp3)$"),
    quality: Optional[str] = Query("best"),
):
    if not SUPPORTED_URL_RE.match(url.strip()):
        raise HTTPException(status_code=400, detail="Invalid or unsupported URL.")

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "starting", "percent": 0, "created": time.time()}

    thread = threading.Thread(
        target=_run_download_job, args=(job_id, url.strip(), format, quality), daemon=True
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/download/progress/{job_id}")
def download_progress(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id.")
        # Don't leak internal file paths to the client
        return {k: v for k, v in job.items() if k not in ("filepath", "job_dir")}


@app.get("/api/download/file/{job_id}")
def download_file(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown job id.")
        if job["status"] == "error":
            raise HTTPException(status_code=422, detail=job.get("error", "Download failed."))
        if job["status"] != "finished":
            raise HTTPException(status_code=409, detail="Not finished yet.")
        filepath = job["filepath"]
        download_name = job["download_name"]
        job_dir = job["job_dir"]

    return FileResponse(
        path=filepath,
        filename=download_name,
        media_type="video/mp4" if download_name.endswith(".mp4") else "audio/mpeg",
        background=BackgroundTask(_cleanup_job, job_id, job_dir),
    )


def _cleanup_job(job_id: str, job_dir: str):
    shutil.rmtree(job_dir, ignore_errors=True)
    with JOBS_LOCK:
        JOBS.pop(job_id, None)