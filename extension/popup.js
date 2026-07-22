// Render handles everything except YouTube (blocked by YouTube's anti-bot on cloud IPs)
const API_BASE_MAIN = "https://viddownloader-a7z9.onrender.com";
// Your local PC, tunneled via ngrok — only used for YouTube links.
// Must be running (uvicorn + `ngrok http --url=... 8000`) for YouTube downloads to work.
const API_BASE_YOUTUBE = "https://garnish-unpopular-parade.ngrok-free.dev";
// Full website (deployed on Vercel) — used for the "open full site" link
const FRONTEND_URL = "https://twitter-video-downloader-extension.vercel.app";
// Bypasses ngrok's one-time browser warning page
const NGROK_HEADERS = [{ name: 'ngrok-skip-browser-warning', value: 'true' }];
const NGROK_FETCH_HEADERS = { 'ngrok-skip-browser-warning': 'true' };

const SUPPORTED_URL_RE = /^https?:\/\/([a-z0-9-]+\.)?(twitter\.com|x\.com|youtube\.com|youtu\.be|instagram\.com|reddit\.com|redd\.it|pinterest\.[a-z.]+|pin\.it|snapchat\.com|sharechat\.com)\/\S+/i;

function isYouTubeUrl(url) {
  return /(youtube\.com|youtu\.be)/i.test(url);
}
function backendFor(url) {
  return isYouTubeUrl(url) ? API_BASE_YOUTUBE : API_BASE_MAIN;
}
function fetchHeadersFor(url) {
  return isYouTubeUrl(url) ? NGROK_FETCH_HEADERS : {};
}
function downloadHeadersFor(url) {
  return isYouTubeUrl(url) ? NGROK_HEADERS : [];
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

const urlBox = document.getElementById('urlBox');
const fmtGroup = document.getElementById('fmtGroup');
const goBtn = document.getElementById('goBtn');
const statusEl = document.getElementById('status');
const openSite = document.getElementById('openSite');

let currentFormat = 'mp4';
let postUrl = null;

function setStatus(msg, isErr = false) {
  statusEl.textContent = msg;
  statusEl.classList.toggle('err', isErr);
}

fmtGroup.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-fmt]');
  if (!btn) return;
  currentFormat = btn.dataset.fmt;
  [...fmtGroup.children].forEach(b => b.classList.toggle('active', b === btn));
});

(async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabUrl = tab?.url || '';

  if (SUPPORTED_URL_RE.test(tabUrl)) {
    postUrl = tabUrl.split('?')[0];
    urlBox.textContent = postUrl;
    urlBox.classList.remove('empty');
    goBtn.disabled = false;
    if (isYouTubeUrl(postUrl)) {
      setStatus('YouTube link — needs your local backend + ngrok tunnel running.');
    }
  } else {
    urlBox.textContent = 'Open a supported post (Twitter/X, YouTube, Instagram, Reddit, Pinterest, Snapchat, ShareChat) to enable this.';
  }

  openSite.addEventListener('click', () => {
    const target = postUrl
      ? `${FRONTEND_URL}?url=${encodeURIComponent(postUrl)}`
      : FRONTEND_URL;
    chrome.tabs.create({ url: target });
  });
})();

goBtn.addEventListener('click', async () => {
  if (!postUrl) return;
  goBtn.disabled = true;

  const base = backendFor(postUrl);
  const fetchHeaders = fetchHeadersFor(postUrl);

  try {
    setStatus('Starting…');
    const startRes = await fetch(
      `${base}/api/download/start?url=${encodeURIComponent(postUrl)}&format=${currentFormat}&quality=best`,
      { method: 'POST', headers: fetchHeaders }
    );
    const startData = await startRes.json();
    if (!startRes.ok) throw new Error(startData.detail || 'Could not start download.');
    const jobId = startData.job_id;

    let finished = false;
    while (!finished) {
      await sleep(700);
      const progRes = await fetch(`${base}/api/download/progress/${jobId}`, { headers: fetchHeaders });
      const prog = await progRes.json();
      if (!progRes.ok) throw new Error(prog.detail || 'Lost track of the download.');

      if (prog.status === 'error') throw new Error(prog.error || 'Download failed.');
      if (prog.status === 'downloading' && prog.percent != null) {
        setStatus(`Downloading… ${Math.round(prog.percent)}%`);
      } else if (prog.status === 'processing') {
        setStatus('Converting…');
      }
      if (prog.status === 'finished') finished = true;
    }

    const fileUrl = `${base}/api/download/file/${jobId}`;
    if (chrome.downloads && chrome.downloads.download) {
      chrome.downloads.download({
        url: fileUrl,
        filename: `snagpost.${currentFormat}`,
        headers: downloadHeadersFor(postUrl)
      });
      setStatus('Download started — check your downloads bar.');
    } else {
      chrome.tabs.create({ url: fileUrl });
      setStatus('Opened download in a new tab.');
    }
  } catch (err) {
    const hint = isYouTubeUrl(postUrl)
      ? ' (Make sure your local backend + ngrok tunnel are running.)'
      : '';
    setStatus((err.message || 'Something went wrong.') + hint, true);
  } finally {
    goBtn.disabled = false;
  }
});