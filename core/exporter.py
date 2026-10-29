"""Export captured images (CBZ / PDF / folder) and text books (EPUB / PDF)."""
from __future__ import annotations

import html
import io
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import img2pdf
from PIL import Image

KEEP_AS_IS = {"jpg", "png"}  # formats every CBZ reader and img2pdf handle


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        cand = path.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def to_jpeg_bytes(data_or_path, quality: int = 92) -> bytes:
    src = io.BytesIO(data_or_path) if isinstance(data_or_path, (bytes, bytearray)) else data_or_path
    with Image.open(src) as im:
        im.seek(0)  # first frame of animated GIF/WebP
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()


def _png_has_alpha(path: Path) -> bool:
    with Image.open(path) as im:
        return im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)


def normalized_pages(images: list[Path], for_pdf: bool = False) -> list[tuple[str, bytes]]:
    """[(ext, bytes)] with webp/avif/gif/etc converted to JPEG (and alpha PNGs for PDF)."""
    pages = []
    for p in images:
        ext = p.suffix.lower().lstrip(".")
        if ext == "jpeg":
            ext = "jpg"
        if ext in KEEP_AS_IS and not (for_pdf and ext == "png" and _png_has_alpha(p)):
            pages.append((ext, p.read_bytes()))
        else:
            pages.append(("jpg", to_jpeg_bytes(p)))
    return pages


def _page_names(count: int) -> list[str]:
    width = max(3, len(str(count)))
    return [f"{i:0{width}d}" for i in range(1, count + 1)]


def export_cbz(images: list[Path], dest: Path) -> Path:
    dest = unique_path(dest.with_suffix(".cbz"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    pages = normalized_pages(images)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for name, (ext, data) in zip(_page_names(len(pages)), pages):
            z.writestr(f"{name}.{ext}", data)
    return dest


def export_image_pdf(images: list[Path], dest: Path) -> Path:
    dest = unique_path(dest.with_suffix(".pdf"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    pages = [data for _, data in normalized_pages(images, for_pdf=True)]
    dest.write_bytes(img2pdf.convert(pages))
    return dest


def export_folder(images: list[Path], dest: Path) -> Path:
    dest = unique_path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    pages = normalized_pages(images)
    for name, (ext, data) in zip(_page_names(len(pages)), pages):
        (dest / f"{name}.{ext}").write_bytes(data)
    return dest


def export_images(images: list[Path], fmt: str, dest_base: Path) -> Path:
    """fmt: cbz | pdf | folder. dest_base has no extension."""
    if not images:
        raise ValueError("no images to export")
    if fmt == "pdf":
        return export_image_pdf(images, dest_base)
    if fmt == "folder":
        return export_folder(images, dest_base)
    return export_cbz(images, dest_base)


# --------------------------------------------------------------------------- text books

@dataclass
class BookChapter:
    title: str
    body_html: str                      # sanitized XHTML fragment; <img src="images/xxx">
    images: dict[str, Path] = field(default_factory=dict)   # "images/xxx" -> local file


@dataclass
class Book:
    title: str
    author: str = ""
    language: str = "en"
    source_url: str = ""
    chapters: list[BookChapter] = field(default_factory=list)


BOOK_CSS = """
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.55; margin: 0 4%; }
h1, h2, h3, h4 { font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.25; }
h1.chapter-title { margin-top: 1.5em; page-break-before: always; break-before: page; }
p { margin: 0 0 0.8em 0; text-align: justify; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
blockquote { margin: 1em 2em; font-style: italic; }
pre, code { font-family: Consolas, monospace; white-space: pre-wrap; }
.titlepage { text-align: center; margin-top: 30%; }
.toc ol { line-height: 1.8; }
.toc a { text-decoration: none; color: inherit; }
"""


def _epub_image(path: Path) -> tuple[bytes, str, str]:
    """Returns (data, media_type, ext) using EPUB-safe formats."""
    ext = path.suffix.lower().lstrip(".")
    if ext in ("jpg", "jpeg"):
        return path.read_bytes(), "image/jpeg", "jpg"
    if ext == "png":
        return path.read_bytes(), "image/png", "png"
    if ext == "gif":
        return path.read_bytes(), "image/gif", "gif"
    return to_jpeg_bytes(path), "image/jpeg", "jpg"


def export_epub(book: Book, dest: Path) -> Path:
    from ebooklib import epub

    dest = unique_path(dest.with_suffix(".epub"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    eb = epub.EpubBook()
    eb.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    eb.set_title(book.title or "Untitled")
    eb.set_language(book.language or "en")
    if book.author:
        eb.add_author(book.author)
    if book.source_url:
        eb.add_metadata("DC", "source", book.source_url)
    css = epub.EpubItem(uid="style", file_name="style/book.css", media_type="text/css", content=BOOK_CSS)
    eb.add_item(css)

    items = []
    for i, ch in enumerate(book.chapters, 1):
        body = ch.body_html
        for ref, path in ch.images.items():
            data, media, ext = _epub_image(path)
            new_ref = str(Path(ref).with_suffix("." + ext)).replace("\\", "/")
            if new_ref != ref:
                body = body.replace(f'src="{ref}"', f'src="{new_ref}"')
            eb.add_item(epub.EpubItem(uid=f"img_{i}_{Path(new_ref).stem}", file_name=new_ref,
                                      media_type=media, content=data))
        item = epub.EpubHtml(title=ch.title or f"Chapter {i}", file_name=f"chap_{i:04d}.xhtml",
                             lang=book.language or "en")
        item.content = f'<h1 class="chapter-title">{html.escape(ch.title)}</h1>\n{body}'
        item.add_item(css)
        eb.add_item(item)
        items.append(item)

    eb.toc = tuple(epub.Link(it.file_name, it.title, f"toc_{n}") for n, it in enumerate(items, 1))
    eb.add_item(epub.EpubNcx())
    eb.add_item(epub.EpubNav())
    eb.spine = ["nav", *items]
    epub.write_epub(str(dest), eb, {})
    return dest


def book_html(book: Book, image_root: Optional[Path] = None) -> str:
    """Single HTML document (title page, TOC, chapters) used for PDF rendering."""
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(book.title)}</title><style>{BOOK_CSS}</style></head><body>",
        f"<div class='titlepage'><h1>{html.escape(book.title)}</h1>",
        f"<h3>{html.escape(book.author)}</h3>" if book.author else "",
        "</div><div class='toc' style='break-before: page'><h2>Contents</h2><ol>",
    ]
    for i, ch in enumerate(book.chapters, 1):
        parts.append(f"<li><a href='#ch{i}'>{html.escape(ch.title)}</a></li>")
    parts.append("</ol></div>")
    for i, ch in enumerate(book.chapters, 1):
        body = ch.body_html
        for ref, path in ch.images.items():
            body = body.replace(f'src="{ref}"', f'src="{path.resolve().as_uri()}"')
        parts.append(f"<h1 class='chapter-title' id='ch{i}'>{html.escape(ch.title)}</h1>{body}")
    parts.append("</body></html>")
    return "".join(parts)


def export_text_pdf(book: Book, dest: Path, session, workdir: Path) -> Path:
    """Render the book with headless Chromium (page.pdf) - keeps headings, images and a PDF outline."""
    dest = unique_path(dest.with_suffix(".pdf"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "book.html"
    src.write_text(book_html(book), encoding="utf-8")
    page = session.headless_page()
    try:
        page.goto(src.resolve().as_uri(), wait_until="load")
        footer = ("<div style='font-size:8px;width:100%;text-align:center;color:#888'>"
                  "<span class='pageNumber'></span></div>")
        page.pdf(path=str(dest), format="A4", print_background=True, outline=True, tagged=True,
                 display_header_footer=True, header_template="<div></div>", footer_template=footer,
                 margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
    finally:
        page.close()
    return dest


def save_file(data: bytes, dest: Path) -> Path:
    dest = unique_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
