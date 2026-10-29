"""Ebook mode: page-type detection, original-file discovery, text chapter extraction."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from html import escape as html_escape

from lxml import etree, html as lhtml

FILE_EXTS = (".pdf", ".epub")
MIN_TEXT_CHARS = 1200

DRM_PATTERNS = [
    (re.compile(r"(^|\.)read\.amazon\.[a-z.]+$"), "Kindle Cloud Reader"),
    (re.compile(r"(^|\.)amazon\.[a-z.]+/kindle-dbs/"), "Kindle"),
    (re.compile(r"^play\.google\.com/(store/)?books"), "Google Play Books"),
    (re.compile(r"^books\.google\.[a-z.]+/books/reader"), "Google Play Books"),
    (re.compile(r"(^|\.)read\.kobo\.com$"), "Kobo reader"),
    (re.compile(r"(^|\.)books\.apple\.com$"), "Apple Books"),
]


class DrmProtectedError(RuntimeError):
    pass


def drm_reader_name(url: str) -> Optional[str]:
    u = urlparse(url)
    host = (u.netloc or "").lower().split(":")[0]
    for pat, name in DRM_PATTERNS:
        if pat.search(host) or pat.search(host + u.path.lower()):
            return name
    return None


def check_drm(url: str) -> None:
    name = drm_reader_name(url)
    if name:
        raise DrmProtectedError(
            f"{name} uses DRM-protected books. Comic Downloader can't and won't download them - "
            "use the official app to read your purchase."
        )


_FILE_PATH_RE = re.compile(r"\.(pdf|epub)(?:$|[.\d])")


def looks_like_file_url(url: str) -> Optional[str]:
    """'.pdf'/'.epub' if the URL path names such a file (also e.g. Gutenberg's '84.epub3.images')."""
    path = unquote(urlparse(url).path).lower()
    m = _FILE_PATH_RE.search(path.rsplit("/", 1)[-1])
    return "." + m.group(1) if m else None


def sniff_file(data: bytes) -> Optional[str]:
    """'.pdf' / '.epub' from magic bytes, else None."""
    if data[:5] == b"%PDF-":
        return ".pdf"
    if data[:2] == b"PK" and b"application/epub+zip" in data[:200]:
        return ".epub"
    return None


FILE_LINKS_JS = r"""
() => {
  const out = [];
  const abs = (u) => { try { return new URL(u, document.baseURI).href; } catch (e) { return null; } };
  for (const el of document.querySelectorAll('iframe[src], embed[src], object[data], frame[src]')) {
    out.push({kind: 'embedded', url: abs(el.getAttribute('src') || el.getAttribute('data')), text: ''});
  }
  for (const a of document.querySelectorAll('a[href]')) {
    out.push({kind: 'link', url: a.href, text: ((a.textContent || '') + ' ' + (a.getAttribute('download') !== null ? 'download' : '')
              + ' ' + (a.getAttribute('title') || '')).replace(/\s+/g, ' ').trim().slice(0, 100)});
  }
  return out;
}
"""

_VIEWER_PARAMS = ("file", "url", "src", "pdf", "doc", "document")


def _file_from_viewer(url: str) -> Optional[str]:
    """pdf.js / Google Docs viewer / generic ?file= viewers -> the real file URL."""
    try:
        q = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    for key in _VIEWER_PARAMS:
        for val in q.get(key, []):
            target = urljoin(url, unquote(val))
            if looks_like_file_url(target) or "pdf" in target.lower():
                return target
    frag = urlparse(url).fragment
    if frag.startswith("file="):
        return urljoin(url, unquote(frag[5:]))
    return None


@dataclass
class FileCandidates:
    embedded: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def find_file_candidates(page) -> FileCandidates:
    """Original PDF/EPUB URLs from embedded viewers and download links, best first."""
    res = FileCandidates()
    try:
        raw = page.evaluate(FILE_LINKS_JS)
    except Exception:
        return res
    scored_links = []
    for item in raw:
        url = item.get("url")
        if not url or not url.startswith(("http:", "https:")):
            continue
        if item["kind"] == "embedded":
            target = url if looks_like_file_url(url) else _file_from_viewer(url)
            if target and target not in res.embedded:
                res.embedded.append(target)
        else:
            ext = looks_like_file_url(url) or (looks_like_file_url(_file_from_viewer(url) or "") and "viewer")
            if not ext:
                continue
            target = url if ext != "viewer" else _file_from_viewer(url)
            text = item.get("text", "").lower()
            score = (2 if "download" in text else 0) + (1 if ext == ".epub" else 0)
            scored_links.append((score, target))
    # Frames too: an embedded viewer whose own document is the PDF.
    for frame in page.frames[1:]:
        if looks_like_file_url(frame.url) and frame.url not in res.embedded:
            res.embedded.append(frame.url)
    seen = set(res.embedded)
    for _, url in sorted(scored_links, key=lambda x: -x[0]):
        if url not in seen:
            seen.add(url)
            res.links.append(url)
    return res


def filename_for(url: str, content_disposition: str = "", ext: str = "") -> str:
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)|filename=\"?([^\";]+)\"?", content_disposition or "", re.I)
    name = unquote((m.group(1) or m.group(2)).strip()) if m else ""
    if not name:
        name = unquote(Path(urlparse(url).path).name) or "book"
    if ext and not name.lower().endswith(ext):
        name = Path(name).stem + ext
    return name


# --------------------------------------------------------------------------- text chapters

ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "img", "ul", "ol", "li", "blockquote",
    "em", "i", "strong", "b", "u", "s", "sub", "sup", "pre", "code", "table", "thead", "tbody",
    "tr", "td", "th", "figure", "figcaption", "div", "span",
}
RENAME = {"head": "h3", "list": "ul", "item": "li", "quote": "blockquote", "hi": "em", "lb": "br"}


@dataclass
class TextChapter:
    title: str
    body_html: str
    image_urls: list[str]
    text_len: int
    book_title: str = ""
    author: str = ""
    language: str = ""


def _meta(doc, *names: str) -> str:
    for n in names:
        for attr in ("property", "name", "itemprop"):
            vals = doc.xpath(f'//meta[@{attr}="{n}"]/@content')
            if vals and vals[0].strip():
                return vals[0].strip()
    return ""


def extract_text_chapter(html_text: str, url: str) -> Optional[TextChapter]:
    import trafilatura

    content = trafilatura.extract(
        html_text, url=url, output_format="html", include_images=True, include_formatting=True,
        include_links=False, include_tables=True, include_comments=False, favor_recall=True,
    )
    if not content:
        return None
    frag = lhtml.fromstring(content)
    body = frag.find(".//body")
    body = body if body is not None else frag

    image_urls: list[str] = []
    for el in list(body.iter()):
        if not isinstance(el.tag, str):
            el.drop_tree()
            continue
        tag = RENAME.get(el.tag, el.tag)
        if el is body:
            continue
        if tag not in ALLOWED_TAGS:
            el.drop_tag()
            continue
        el.tag = tag
        keep = {}
        if tag == "img":
            src = el.get("src") or el.get("data-src") or ""
            if not src or (src.startswith("data:") and len(src) < 1500):
                el.drop_tree()
                continue
            src = urljoin(url, src)
            keep = {"src": src, "alt": el.get("alt", "")}
            image_urls.append(src)
        for k in list(el.attrib):
            del el.attrib[k]
        for k, v in keep.items():
            el.set(k, v)

    text_len = len(re.sub(r"\s+", " ", body.text_content()).strip())

    doc = lhtml.fromstring(html_text)
    page_title = (doc.findtext(".//title") or "").strip()
    meta = trafilatura.extract_metadata(html_text, default_url=url)
    title = ""
    first_heading = next((h for h in body.iter("h1", "h2") if h.text_content().strip()), None)
    if first_heading is not None:
        title = first_heading.text_content().strip()
        first_heading.drop_tree()  # exporter adds its own chapter heading
    title = title or (meta.title if meta and meta.title else "") or page_title or "Chapter"

    book_title = _meta(doc, "og:novel:book_name", "book:title", "og:book:title")
    author = _meta(doc, "og:novel:author", "book:author", "author", "article:author") or \
        (meta.author if meta and meta.author else "")
    language = (doc.get("lang") or (meta.language if meta and meta.language else "") or "en").split("-")[0]

    inner = "".join(
        etree.tostring(child, method="xml", encoding="unicode") for child in body
    )
    if body.text and body.text.strip():
        inner = f"<p>{html_escape(body.text.strip())}</p>" + inner
    return TextChapter(title=title[:200], body_html=inner, image_urls=image_urls, text_len=text_len,
                       book_title=book_title or page_title, author=author, language=language or "en")
