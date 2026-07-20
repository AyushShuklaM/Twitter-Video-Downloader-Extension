// Backend API (deployed on Render)
const API_BASE = "https://viddownloader-a7z9.onrender.com";
// Full website (deployed on Vercel) — used for the "open full site" link
const FRONTEND_URL = "https://twitter-video-downloader-extension.vercel.app";

const TWEET_URL_RE = /^https?:\/\/(www\.)?(twitter|x)\.com\/[^/]+\/status(es)?\/\d+/;

const urlBox = document.getElementById('urlBox');
const fmtGroup = document.getElementById('fmtGroup');
const goBtn = document.getElementById('goBtn');
const statusEl = document.getElementById('status');
const openSite = document.getElementById('openSite');

let currentFormat = 'mp4';
let tweetUrl = null;

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

  // Simple check for any http/https URL
  if (/^https?:\/\/.+/.test(tabUrl)) {
    tweetUrl = tabUrl.split('?')[0];
    urlBox.textContent = tweetUrl;
    urlBox.classList.remove('empty');
    goBtn.disabled = false;
  } else {
    urlBox.textContent = 'Open a valid webpage to enable this.';
  }


  openSite.addEventListener('click', () => {
    const target = tweetUrl
      ? `${FRONTEND_URL}?url=${encodeURIComponent(tweetUrl)}`
      : FRONTEND_URL;
    chrome.tabs.create({ url: target });
  });
})();

goBtn.addEventListener('click', async () => {
  if (!tweetUrl) return;
  goBtn.disabled = true;
  setStatus('Resolving video…');

  try {
    const downloadUrl =
      `${API_BASE}/api/download?url=${encodeURIComponent(tweetUrl)}&format=${currentFormat}&quality=best`;

    // chrome.downloads requires the "downloads" permission; using a plain
    // tab-open fallback keeps the extension to activeTab-only permissions.
    if (chrome.downloads && chrome.downloads.download) {
      chrome.downloads.download({ url: downloadUrl, filename: `snagpost.${currentFormat}` });
      setStatus('Download started — check your downloads bar.');
    } else {
      chrome.tabs.create({ url: downloadUrl });
      setStatus('Opened download in a new tab.');
    }
  } catch (err) {
    setStatus(err.message || 'Something went wrong.', true);
  } finally {
    goBtn.disabled = false;
  }
});