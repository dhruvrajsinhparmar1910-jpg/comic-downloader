"""Runs queue items: detects the page type and drives scraping + export."""
from __future__ import annotations

import os
import re
import threading
import traceback
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

from . import ebook, exporter, scraper, series
from .browser import BrowserSession, start_session
from .settings import Settings

LogFn = Callable[[str], None]

QUEUED, RUNNING, DONE, ERROR, STOPPED = "queued", "running", "done", "error", "stopped"


@dataclass
class JobItem:
    url: str
    mode: str = "auto"          # auto | comic | ebook
    fmt: str = "cbz"            # cbz | pdf | epub | folder
    whole_series: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = QUEUED
    message: str = ""
    output: str = ""
    progress: Optional[float] = 0.0


class StopRequested(Exception):
    pass


class Engine:
    def __init__(self, settings: Settings, log: LogFn,
                 on_progress: Callable[[JobItem], None] = lambda item: None,
                 stop: Optional[threading.Event] = None):
        self.settings = settings
        self.log = log
        self.on_progress = on_progress
        self.stop = stop or threading.Event()
        self.session: Optional[BrowserSession] = None

    # ------------------------------------------------------------------ helpers

    def _progress(self, item: JobItem, fraction: Optional[float], message: str) -> None:
        item.progress = fraction
        item.message = message
        self.on_progress(item)

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise StopRequested()

    def _delay(self) -> None:
        series.polite_delay(self.settings.delay_min, self.settings.delay_max, self.stop)
        self._check_stop()

    @property
    def out_root(self) -> Path:
        return Path(self.settings.output_dir).expanduser()

    def _tmp_dir(self, item: JobItem) -> Path:
        return self.out_root / ".comicdl-tmp" / item.id

    # ------------------------------------------------------------------ queue

    def run(self, items: Union[list[JobItem], Callable[[], Optional[JobItem]]]) -> None:
        """Process queued items one at a time. `items` may be a list or a callable returning the next item."""
        if isinstance(items, list):
            pending = iter([i for i in items if i.status == QUEUED])
            next_item = lambda: next(pending, None)  # noqa: E731
        else:
            next_item = items
        try:
            first = True
            while not self.stop.is_set():
                item = next_item()
                if item is None:
                    break
                if not first:
                    self._delay_quiet()
                    if self.stop.is_set():
                        break
                first = False
                self.run_item(item)
        finally:
            if self.session is not None:
                self.session.close()
                self.session = None

    def _delay_quiet(self) -> None:
        series.polite_delay(self.settings.delay_min, self.settings.delay_max, self.stop)

    def run_item(self, item: JobItem) -> None:
        item.status = RUNNING
        item.output = ""
        self._progress(item, None, "starting")
        self.log(f"> {item.url}  [mode={item.mode}, format={item.fmt}, series={item.whole_series}]")
        try:
            ebook.check_drm(item.url)
            if self.session is None:
                self._progress(item, None, "starting browser")
                self.session = start_session(self.settings, self.log)
            outputs = self.process(item)
            item.status = DONE
            item.output = str(outputs[0].parent if len(outputs) > 1 else outputs[0])
            self._progress(item, 1.0, f"done - {len(outputs)} file(s)")
            self.log(f"OK - saved to {item.output}")
        except StopRequested:
            item.status = STOPPED
            self._progress(item, 0.0, "stopped")
            self.log("Stopped.")
        except ebook.DrmProtectedError as e:
            item.status = ERROR
            self._progress(item, 0.0, "DRM-protected - skipped")
            self.log(f"ERROR: {e}")
        except Exception as e:
            item.status = ERROR
            if os.environ.get("CD_DEBUG"):
                self.log(traceback.format_exc())
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            self._progress(item, 0.0, f"error: {msg[:120]}")
            self.log(f"ERROR: {msg}")
        finally:
            exporter.remove_tree(self._tmp_dir(item))

    # ------------------------------------------------------------------ dispatch

    def process(self, item: JobItem) -> list[Path]:
        page = self.session.new_page()
        net = scraper.NetworkCollector(page)
        try:
            if item.mode != "comic" and ebook.looks_like_file_url(item.url):
                self.log("  URL is a PDF/EPUB file - downloading it as-is")
                return [self.download_original([item.url], item.url, item)]
            try:
                resp = scraper.goto(page, item.url)
            except scraper.DirectDownload:
                if item.mode == "comic":
                    raise RuntimeError("This URL is a file download, not a reader page. Try Ebook mode.")
                self.log("  URL triggered a file download - saving the original file")
                return [self.download_original([item.url], item.url, item)]
            ebook.check_drm(page.url)
            ctype = ""
            try:
                ctype = (resp.headers.get("content-type") or "").lower() if resp else ""
            except Exception:
                pass
            if item.mode != "comic" and ("application/pdf" in ctype or "epub" in ctype):
                return [self.download_original([page.url], page.url, item)]

            kind, extra = self.detect(page, item)
            self._check_stop()
            if kind == "file":
                return [self.download_original(extra, page.url, item)]
            if kind == "text":
                return [self.run_text(page, net, item, first_chapter=extra)]
            return self.run_images(page, net, item, scrolled=item.mode != "comic")
        finally:
            try:
                page.close()
            except Exception:
                pass

    def detect(self, page, item: JobItem):
        """Return (kind, extra): ('images', None) | ('text', TextChapter) | ('file', [urls])."""
        if item.mode == "comic":
            return "images", None
        s = self.settings
        files = ebook.find_file_candidates(page)
        if files.embedded:
            self.log(f"  embedded document viewer found: {files.embedded[0][:120]}")
            return "file", files.embedded + files.links
        self._progress(item, None, "scrolling page")
        scraper.auto_scroll(page, self.stop)
        self._check_stop()
        cands = scraper.collect_candidates(page, s.min_image_size)
        big = scraper.count_large_images(cands, s.min_image_size)
        chapter = ebook.extract_text_chapter(page.content(), page.url)
        text_len = chapter.text_len if chapter else 0
        self.log(f"  page analysis: {big} large images, {text_len} chars of text, "
                 f"{len(files.links)} PDF/EPUB links")
        if item.mode == "auto":
            if big >= 3 and text_len < 5000:
                return "images", None
            if text_len >= ebook.MIN_TEXT_CHARS:
                return "text", chapter
            if files.links:
                return "file", files.links
            if big >= 1:
                return "images", None
        else:  # ebook
            if files.links:
                return "file", files.links
            if text_len >= ebook.MIN_TEXT_CHARS:
                return "text", chapter
            if big >= 1:
                return "images", None
            if text_len >= 200:
                return "text", chapter
        if big >= 1 or cands:
            return "images", None
        raise RuntimeError("Couldn't find comic pages, book text or a PDF/EPUB file on this page.")

    # ------------------------------------------------------------------ original files

    def download_original(self, urls: list[str], referer: str, item: JobItem) -> Path:
        accept = "application/pdf,application/epub+zip,application/octet-stream;q=0.9,*/*;q=0.8"
        for url in urls:
            self._check_stop()
            ebook.check_drm(url)
            self._progress(item, None, "downloading original file")
            self.log(f"  downloading {url[:150]}")
            headers = scraper.request_headers(self.session.user_agent, referer, accept)
            for attempt in range(3):
                try:
                    resp = self.session.context.request.get(url, headers=headers, timeout=300000)
                    if not resp.ok:
                        self.log(f"  HTTP {resp.status}")
                        if resp.status in (401, 403, 404, 410):
                            break
                        raise RuntimeError(f"HTTP {resp.status}")
                    data = resp.body()
                    ext = ebook.sniff_file(data)
                    if not ext:
                        self.log("  response is not a PDF/EPUB file (login or DRM page?)")
                        break
                    name = ebook.filename_for(resp.url or url, resp.headers.get("content-disposition", ""), ext)
                    dest = exporter.save_file(data, self.out_root / scraper.safe_name(name, "book" + ext))
                    self.log(f"  saved {dest.name} ({len(data) / 1e6:.1f} MB)")
                    return dest
                except StopRequested:
                    raise
                except Exception as e:
                    self.log(f"  attempt {attempt + 1} failed: {str(e).splitlines()[0]}")
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("Could not download the original PDF/EPUB file.")

    # ------------------------------------------------------------------ images

    def _host(self, url: str) -> str:
        return urlparse(url).netloc.replace("www.", "") or "download"

    def run_images(self, page, net, item: JobItem, scrolled: bool) -> list[Path]:
        s = self.settings
        fmt = item.fmt if item.fmt in ("cbz", "pdf", "folder") else "pdf"
        walker = series.ChapterWalker(page.url, item.whole_series, s.max_chapters, self.stop, self.log)
        outputs: list[Path] = []
        series_dir: Optional[Path] = None
        tmp = self._tmp_dir(item)
        for idx, url in walker:
            if idx > 1:
                self._delay()
                net.reset()
                self._progress(item, None, f"chapter {idx}: loading")
                self.log(f"Chapter {idx}: {url}")
                scraper.goto(page, url)
                ebook.check_drm(page.url)
                scrolled = False
            label = f"chapter {idx}" if item.whole_series else "page"

            def prog(frac: float, text: str, label=label) -> None:
                self._progress(item, frac, f"{label}: {text}")

            if not scrolled:
                self._progress(item, None, f"{label}: scrolling")
            res = scraper.capture_page_images(self.session, page, net, tmp / f"{idx:04d}", s.min_image_size,
                                              self.log, prog, self.stop, scrolled=scrolled)
            self._check_stop()
            walker.set_next(series.find_next_chapter(page))
            if not res.files:
                if idx == 1:
                    raise RuntimeError("No comic images found on this page.")
                self.log(f"  chapter {idx}: no images found, skipping")
                continue
            extra = f", {res.skipped_small} small skipped" if res.skipped_small else ""
            self.log(f"  {len(res.files)} images captured{extra}")
            if item.whole_series:
                if series_dir is None:
                    series_dir = exporter.unique_path(
                        self.out_root / scraper.safe_name(series.series_title(res.title, self._host(url))))
                dest = series_dir / series.chapter_name(idx)
            else:
                dest = self.out_root / scraper.safe_name(res.title or self._host(url))
            self._progress(item, None, f"{label}: exporting {fmt.upper()}")
            out = exporter.export_images(res.files, fmt, dest)
            outputs.append(out)
            self.log(f"  -> {out}")
            exporter.remove_tree(tmp / f"{idx:04d}")
        if not outputs:
            raise RuntimeError("Nothing was downloaded.")
        return outputs

    # ------------------------------------------------------------------ text

    def run_text(self, page, net, item: JobItem, first_chapter) -> Path:
        s = self.settings
        fmt = "pdf" if item.fmt == "pdf" else "epub"
        walker = series.ChapterWalker(page.url, item.whole_series, s.max_chapters, self.stop, self.log)
        book = exporter.Book(title="", source_url=page.url)
        tmp = self._tmp_dir(item)
        img_dir = tmp / "images"
        img_dir.mkdir(parents=True, exist_ok=True)
        for idx, url in walker:
            if idx > 1:
                self._delay()
                net.reset()
                self._progress(item, None, f"chapter {idx}: loading")
                self.log(f"Chapter {idx}: {url}")
                scraper.goto(page, url)
                ebook.check_drm(page.url)
                scraper.auto_scroll(page, self.stop)
                ch = ebook.extract_text_chapter(page.content(), page.url)
            else:
                ch = first_chapter
            self._check_stop()
            walker.set_next(series.find_next_chapter(page))
            if not ch or ch.text_len < 200:
                self.log(f"  chapter {idx}: no readable text, skipping")
                continue
            if not book.chapters:
                full = ch.book_title or ch.title
                name = series.series_title(full, self._host(url))
                if item.whole_series or not ch.title:
                    book.title = name
                else:
                    book.title = ch.title if name.lower() in ch.title.lower() else f"{name} - {ch.title}"
                book.author = ch.author
                book.language = ch.language or "en"
            body = ch.body_html
            images: dict[str, Path] = {}
            for k, img_url in enumerate(ch.image_urls, 1):
                self._progress(item, k / max(len(ch.image_urls), 1), f"chapter {idx}: image {k}")
                data = scraper.get_image_bytes(self.session, page, net, img_url, page.url, self.log)
                info = scraper.image_info(data) if data else None
                attr = xml_escape(img_url, {'"': "&quot;"})
                if not info:
                    body = re.sub(r'<img [^>]*src="%s"[^>]*/>' % re.escape(attr), "", body)
                    continue
                ref = f"images/c{idx:03d}_{k:03d}.{info[0]}"
                path = img_dir / Path(ref).name
                path.write_bytes(data)
                images[ref] = path
                body = body.replace(f'src="{attr}"', f'src="{ref}"')
            book.chapters.append(exporter.BookChapter(title=ch.title or series.chapter_name(idx),
                                                      body_html=body, images=images))
            self._progress(item, None, f"chapter {idx}: {ch.title[:60]}")
            self.log(f"  chapter {idx}: '{ch.title[:80]}' ({ch.text_len} chars, {len(images)} images)")
        if not book.chapters:
            raise RuntimeError("No readable chapter text found.")
        dest = self.out_root / scraper.safe_name(book.title or self._host(page.url))
        self._progress(item, None, f"writing {fmt.upper()}")
        if fmt == "epub":
            out = exporter.export_epub(book, dest)
        else:
            out = exporter.export_text_pdf(book, dest, self.session, tmp)
        self.log(f"  -> {out} ({len(book.chapters)} chapters)")
        return out


def open_login_window(settings: Settings, url: str, log: LogFn, stop: Optional[threading.Event] = None) -> None:
    """Open a visible browser with the saved profile so the user can log in; returns when it's closed."""
    from dataclasses import replace

    login_settings = replace(settings, use_profile=True, show_browser=True)
    session = start_session(login_settings, log, headless=False)
    try:
        page = session.context.pages[0] if session.context.pages else session.new_page()
        if url:
            try:
                page.goto(url)
            except Exception as e:
                log(f"Login window: {str(e).splitlines()[0]}")
        log("Log in in the browser window, then close it. Cookies are saved to your profile folder.")
        while session.context.pages and not (stop and stop.is_set()):
            try:
                session.context.pages[0].wait_for_timeout(500)
            except Exception:
                break
    finally:
        session.close()
        log("Login window closed.")
