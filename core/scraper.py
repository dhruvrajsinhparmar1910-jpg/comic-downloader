"""Page loading, lazy-load scrolling and image capture/download."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from PIL import Image

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]

AD_DOMAINS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "adservice.google.",
    "google-analytics.com", "googletagmanager.com", "googletagservices.com", "amazon-adsystem.com",
    "adnxs.com", "adsrvr.org", "taboola.com", "outbrain.com", "criteo.com", "criteo.net",
    "pubmatic.com", "rubiconproject.com", "openx.net", "casalemedia.com", "moatads.com",
    "scorecardresearch.com", "quantserve.com", "facebook.com/tr", "facebook.net", "ads-twitter.com",
    "adsafeprotected.com", "exoclick.com", "juicyads.com", "propellerads.com", "popads.net",
    "popcash.net", "adcash.com", "mgid.com", "revcontent.com", "zedo.com", "yieldmo.com",
    "33across.com", "sharethrough.com", "media.net", "smartadserver.com", "bidswitch.net",
    "adform.net", "2mdn.net", "gravatar.com", "disqus.com", "disquscdn.com", "addthis.com",
    "sharethis.com", "pixel.wp.com", "stats.wp.com", "hotjar.com", "clarity.ms",
)

IMAGE_EXT_BY_FORMAT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "AVIF": "avif", "BMP": "bmp"}


def is_ad_url(url: str) -> bool:
    if url.startswith(("data:", "blob:")):
        return False
    try:
        u = urlparse(url)
    except ValueError:
        return True
    host_path = (u.netloc + u.path).lower()
    return any(d in host_path for d in AD_DOMAINS)


# --------------------------------------------------------------------------- navigation

class DirectDownload(Exception):
    """Navigation triggered a file download instead of a page (e.g. a PDF/EPUB link)."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


def goto(page, url: str):
    """Navigate and wait for DOM; raises DirectDownload when the URL is a file download."""
    from playwright.sync_api import Error as PwError

    try:
        resp = page.goto(url, wait_until="domcontentloaded")
    except PwError as e:
        if "Download is starting" in str(e) or "net::ERR_ABORTED" in str(e):
            raise DirectDownload(url) from e
        raise
    try:
        page.wait_for_load_state("load", timeout=15000)
    except Exception:
        pass
    return resp


def wait_network_idle(page, timeout: float = 10.0) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
    except Exception:
        pass


SCROLL_JS = r"""
(step) => {
  const doc = document.scrollingElement || document.documentElement;
  let target = doc;
  if (doc.scrollHeight <= window.innerHeight + 50) {
    // Some readers scroll inside a container instead of the window.
    let best = null, bestH = 0;
    for (const el of document.querySelectorAll('body *')) {
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      if (el.scrollHeight > el.clientHeight + 50 && el.scrollHeight > bestH) { best = el; bestH = el.scrollHeight; }
    }
    if (best) target = best;
  }
  const view = target === doc ? window.innerHeight : target.clientHeight;
  target.scrollBy(0, Math.max(200, view * step));
  const top = target === doc ? window.scrollY : target.scrollTop;
  return {atBottom: top + view >= target.scrollHeight - 5, height: target.scrollHeight};
}
"""


def auto_scroll(page, stop: Optional[threading.Event] = None, pause: float = 0.35,
                max_steps: int = 600) -> None:
    """Scroll down slowly until the page stops growing, to trigger lazy-loading."""
    stable = 0
    last_h = -1
    for _ in range(max_steps):
        if stop is not None and stop.is_set():
            return
        try:
            r = page.evaluate(SCROLL_JS, 0.8)
        except Exception:
            return
        time.sleep(pause)
        if r["atBottom"]:
            stable = stable + 1 if r["height"] == last_h else 0
            if stable >= 3:
                break
            time.sleep(pause)  # give lazy loaders a moment at the bottom
        last_h = r["height"]
    wait_network_idle(page)


# --------------------------------------------------------------------------- collection

COLLECT_JS = r"""
() => {
  const out = [];
  const abs = (u) => { if (!u) return null; u = u.trim(); if (!u || u.startsWith('#')) return null;
                       try { return new URL(u, document.baseURI).href; } catch (e) { return null; } };
  const fromSrcset = (s) => {
    if (!s) return null;
    let best = null, bw = -1;
    for (const part of s.split(/,\s+(?=\S)/)) {
      const bits = part.trim().split(/\s+/);
      if (!bits[0]) continue;
      let w = 1;
      const m = (bits[1] || '').match(/^([\d.]+)([wx])$/);
      if (m) w = parseFloat(m[1]) * (m[2] === 'x' ? 1000 : 1);
      if (w > bw) { bw = w; best = bits[0]; }
    }
    return best;
  };
  const tiny = (u) => u && u.startsWith('data:') && u.length < 1500;   // 1x1 placeholders
  const lazyAttrs = ['data-src', 'data-lazy-src', 'data-original', 'data-url', 'data-lazy', 'data-full', 'data-hi-res'];
  const push = (url, el, kind, nw, nh) => {
    if (!url || tiny(url)) return;
    const r = el.getBoundingClientRect();
    out.push({url, kind, nw: nw || 0, nh: nh || 0, rw: Math.round(r.width), rh: Math.round(r.height),
              alt: (el.getAttribute && (el.getAttribute('alt') || el.getAttribute('title'))) || ''});
  };
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName;
    if (tag === 'IMG') {
      const cands = [];
      for (const a of lazyAttrs) cands.push(abs(el.getAttribute(a)));
      cands.push(abs(fromSrcset(el.getAttribute('data-srcset') || el.getAttribute('data-lazy-srcset'))));
      if (el.parentElement && el.parentElement.tagName === 'PICTURE') {
        cands.unshift(el.currentSrc || null);
        for (const s of el.parentElement.querySelectorAll('source'))
          cands.push(abs(fromSrcset(s.getAttribute('srcset') || s.getAttribute('data-srcset'))));
      }
      cands.push(abs(fromSrcset(el.getAttribute('srcset'))));
      cands.push(el.currentSrc || null, abs(el.getAttribute('src')));
      const url = cands.find((u) => u && !tiny(u));
      // natural size only describes the currently loaded source
      const loaded = url && (url === el.currentSrc || url === el.src);
      push(url, el, 'img', loaded ? el.naturalWidth : 0, loaded ? el.naturalHeight : 0);
      continue;
    }
    if (tag === 'SOURCE' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') continue;
    if (tag === 'IMAGE' && el.closest('svg')) {   // <svg><image href=...>
      push(abs(el.getAttribute('href') || el.getAttribute('xlink:href')), el, 'svg', 0, 0);
      continue;
    }
    const bgAttr = el.getAttribute('data-bg') || el.getAttribute('data-background') || el.getAttribute('data-background-image');
    if (bgAttr) push(abs(bgAttr.replace(/^url\(["']?|["']?\)$/g, '')), el, 'bg', 0, 0);
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg !== 'none') {
      for (const m of bg.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)) push(abs(m[1]), el, 'bg', 0, 0);
    }
  }
  return out;
}
"""


@dataclass
class ImageCandidate:
    url: str
    kind: str = "img"
    nw: int = 0
    nh: int = 0
    rw: int = 0
    rh: int = 0
    alt: str = ""


def collect_candidates(page, min_size: int) -> list[ImageCandidate]:
    """Image URLs in DOM order, deduped, without ads and known-small images."""
    raw = page.evaluate(COLLECT_JS)
    seen: set[str] = set()
    result: list[ImageCandidate] = []
    for item in raw:
        c = ImageCandidate(**item)
        key = c.url.split("#")[0]
        if key in seen or not key.startswith(("http:", "https:", "data:", "blob:")):
            continue
        seen.add(key)
        if is_ad_url(c.url):
            continue
        if c.nw and c.nh and min(c.nw, c.nh) < min_size:
            continue
        # Rendered tiny and not loaded yet: almost certainly an icon/avatar.
        if not c.nw and 0 < max(c.rw, c.rh) < 64 and c.kind != "img":
            continue
        result.append(c)
    return result


class NetworkCollector:
    """Records image responses in arrival order (fallback source + body cache)."""

    def __init__(self, page):
        self.responses: dict[str, object] = {}
        self.order: list[str] = []
        self.main_responses: list[object] = []
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            rtype = response.request.resource_type
            ctype = (response.headers.get("content-type") or "").lower()
        except Exception:
            return
        if rtype == "document":
            self.main_responses.append(response)
        if response.status != 200 or not (ctype.startswith("image/") or rtype == "image"):
            return
        url = response.url
        if url not in self.responses:
            self.order.append(url)
        self.responses[url] = response

    def body(self, url: str) -> Optional[bytes]:
        resp = self.responses.get(url)
        if resp is None:
            return None
        try:
            return resp.body()
        except Exception:
            return None

    def reset(self) -> None:
        self.responses.clear()
        self.order.clear()
        self.main_responses.clear()


# --------------------------------------------------------------------------- download

BLOB_JS = r"""
async (u) => {
  const r = await fetch(u);
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return btoa(s);
}
"""


def request_headers(user_agent: str, referer: str, accept: str = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8") -> dict:
    return {"User-Agent": user_agent, "Referer": referer, "Accept": accept}


def fetch_bytes(session, url: str, referer: str, retries: int = 3, accept: Optional[str] = None,
                log: Optional[LogFn] = None) -> Optional[bytes]:
    """GET with the browser's cookies plus Referer/User-Agent; retry with backoff."""
    headers = request_headers(session.user_agent, referer) if accept is None else \
        request_headers(session.user_agent, referer, accept)
    last = ""
    for attempt in range(retries):
        try:
            resp = session.context.request.get(url, headers=headers, timeout=60000)
            if resp.ok:
                return resp.body()
            last = f"HTTP {resp.status}"
            if resp.status in (401, 403, 404, 410):
                break
        except Exception as e:  # network errors, timeouts
            last = str(e).splitlines()[0]
        time.sleep(1.5 * (attempt + 1))
    if log:
        log(f"  failed: {url[:120]} ({last})")
    return None


def get_image_bytes(session, page, net: Optional[NetworkCollector], url: str, referer: str,
                    log: Optional[LogFn] = None) -> Optional[bytes]:
    if url.startswith("data:"):
        try:
            header, data = url.split(",", 1)
            return base64.b64decode(data) if ";base64" in header else data.encode()
        except ValueError:
            return None
    if url.startswith("blob:"):
        try:
            return base64.b64decode(page.evaluate(BLOB_JS, url))
        except Exception:
            return None
    if net is not None:
        body = net.body(url)
        if body:
            return body
    return fetch_bytes(session, url, referer, log=log)


def image_info(data: bytes) -> Optional[tuple[str, int, int]]:
    """(extension, width, height) or None if the bytes aren't a decodable image."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            w, h = im.size
    except Exception:
        return None  # SVG, HTML error pages, truncated data
    return IMAGE_EXT_BY_FORMAT.get(fmt, "img"), w, h


@dataclass
class CaptureResult:
    files: list[Path] = field(default_factory=list)
    title: str = ""
    skipped_small: int = 0
    failed: int = 0
    used_network_fallback: bool = False


def download_images(session, page, net: Optional[NetworkCollector], urls: list[str], workdir: Path,
                    min_size: int, log: LogFn, progress: Optional[ProgressFn] = None,
                    stop: Optional[threading.Event] = None, start_index: int = 1,
                    seen_hashes: Optional[set] = None) -> CaptureResult:
    workdir.mkdir(parents=True, exist_ok=True)
    res = CaptureResult()
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    referer = page.url
    idx = start_index
    total = len(urls)
    for n, url in enumerate(urls, 1):
        if stop is not None and stop.is_set():
            break
        if progress:
            progress((n - 1) / max(total, 1), f"image {n}/{total}")
        data = get_image_bytes(session, page, net, url, referer, log)
        if not data:
            res.failed += 1
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen_hashes:
            continue
        info = image_info(data)
        if info is None:
            continue
        ext, w, h = info
        if min(w, h) < min_size:
            res.skipped_small += 1
            continue
        seen_hashes.add(digest)
        path = workdir / f"{idx:04d}.{ext}"
        path.write_bytes(data)
        res.files.append(path)
        idx += 1
    if progress:
        progress(1.0, f"{len(res.files)} images")
    return res


def capture_page_images(session, page, net: NetworkCollector, workdir: Path, min_size: int,
                        log: LogFn, progress: Optional[ProgressFn] = None,
                        stop: Optional[threading.Event] = None, scrolled: bool = False) -> CaptureResult:
    """Full comic capture for the currently loaded page."""
    if not scrolled:
        auto_scroll(page, stop)
    cands = collect_candidates(page, min_size)
    log(f"  found {len(cands)} candidate images in the page")
    res = download_images(session, page, net, [c.url for c in cands], workdir, min_size, log, progress, stop)
    if not res.files and not (stop and stop.is_set()):
        # Fallback: whatever images the browser actually received over the network.
        net_urls = [u for u in net.order if not is_ad_url(u)]
        if net_urls:
            log(f"  no usable DOM images; trying {len(net_urls)} images seen on the network")
            res = download_images(session, page, net, net_urls, workdir, min_size, log, progress, stop)
            res.used_network_fallback = True
    try:
        res.title = page.title()
    except Exception:
        pass
    return res


def count_large_images(cands: list[ImageCandidate], min_size: int) -> int:
    """Images that are known (from natural or rendered size) to be page-sized."""
    n = 0
    for c in cands:
        w, h = (c.nw, c.nh) if c.nw else (c.rw, c.rh)
        if min(w, h) >= min_size:
            n += 1
    return n


_SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_name(name: str, fallback: str = "download", limit: int = 120) -> str:
    name = _SAFE_RE.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?i)(COM|LPT)\d", name):
        name = "_" + name
    return (name[:limit].rstrip(" .") or fallback)
