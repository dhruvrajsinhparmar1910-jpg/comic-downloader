"""Series mode: find the "Next chapter" link and walk chapters."""
from __future__ import annotations

import random
import re
import threading
import time
from typing import Callable, Iterator, Optional

NEXT_JS = r"""
() => {
  const cur = location.href.split('#')[0];
  const ok = (h) => h && /^https?:/i.test(h) && h.split('#')[0] !== cur;
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const ARROWS = '›»→>⟩❯▶⇒';
  const arrowOnly = new RegExp('^[' + ARROWS + '\\s]+$');
  const strip = new RegExp('^[' + ARROWS + '\\s:|\\-]+|[' + ARROWS + '\\s:|\\-]+$', 'g');
  let best = null, bestScore = 0;
  const consider = (href, score) => { if (ok(href) && score > bestScore) { best = href; bestScore = score; } };

  for (const a of document.querySelectorAll('a[href]')) {
    const rel = (a.getAttribute('rel') || '').toLowerCase().split(/\s+/);
    const imgAlt = Array.from(a.querySelectorAll('img')).map((i) => i.alt || i.title).join(' ');
    const texts = [norm(a.textContent), norm(a.getAttribute('aria-label')), norm(a.getAttribute('title')), norm(imgAlt)];
    let score = 0;
    for (const t of texts) {
      if (!t || t.length > 60) continue;
      const core = t.replace(strip, '').toLowerCase();
      if (/^next\s*(chapter|ch\.?|episode|ep\.?|issue|part|chap)\b/.test(core)) score = Math.max(score, 100);
      else if (core === 'next') score = Math.max(score, 60);
      else if (arrowOnly.test(t)) score = Math.max(score, 40);
    }
    if (rel.includes('next')) score = Math.max(score, 80);
    // Links that are disabled or hidden are not real "next" buttons.
    if (score && (a.classList.contains('disabled') || a.getAttribute('aria-disabled') === 'true')) score = 0;
    if (score && a.offsetParent === null && getComputedStyle(a).position !== 'fixed') score -= 30;
    consider(a.href, score);
  }
  const link = document.querySelector('link[rel~="next" i][href]');
  if (link) consider(link.href, 70);
  return best;
}
"""


def find_next_chapter(page) -> Optional[str]:
    try:
        return page.evaluate(NEXT_JS)
    except Exception:
        return None


def chapter_name(index: int) -> str:
    return f"Chapter {index:03d}"


def polite_delay(delay_min: float, delay_max: float, stop: Optional[threading.Event] = None) -> None:
    wait = random.uniform(delay_min, max(delay_min, delay_max))
    if stop is not None:
        stop.wait(wait)
    else:
        time.sleep(wait)


_CHAPTER_RE = re.compile(r"\b(chapter|chap|ch|episode|ep|issue|vol|volume|part|page)\b\.?\s*#?\d", re.I)


def series_title(page_title: str, fallback: str) -> str:
    """Strip "Chapter 12" / site-name parts from a chapter title to get the series name."""
    parts = [p.strip() for p in re.split(r"\s+[|\-–—:»]\s+|\s*\|\s*", page_title or "") if p.strip()]
    for p in parts:
        cleaned = _CHAPTER_RE.split(p)[0].strip(" -–—:,#")
        if cleaned and not re.fullmatch(r"(?i)read|online|free|manga|comic|chapter", cleaned):
            return cleaned
    return fallback


class ChapterWalker:
    """Iterates chapter URLs; caller reports each chapter's next link via `set_next`."""

    def __init__(self, start_url: str, whole_series: bool, max_chapters: int,
                 stop: Optional[threading.Event] = None, log: Callable[[str], None] = print):
        self.start_url = start_url
        self.whole_series = whole_series
        self.max_chapters = max_chapters if whole_series else 1
        self.stop = stop
        self.log = log
        self._next: Optional[str] = None
        self._visited: set[str] = set()

    def set_next(self, url: Optional[str]) -> None:
        self._next = url

    def __iter__(self) -> Iterator[tuple[int, str]]:
        url: Optional[str] = self.start_url
        index = 1
        while url and index <= self.max_chapters:
            if self.stop is not None and self.stop.is_set():
                return
            self._visited.add(url.split("#")[0])
            self._next = None
            yield index, url
            if not self.whole_series:
                return
            nxt = self._next
            if not nxt:
                self.log("No 'next chapter' link found - series finished.")
                return
            if nxt.split("#")[0] in self._visited:
                self.log("Next link points to an already downloaded chapter - stopping.")
                return
            if index >= self.max_chapters:
                self.log(f"Reached the max-chapters limit ({self.max_chapters}).")
                return
            url = nxt
            index += 1
