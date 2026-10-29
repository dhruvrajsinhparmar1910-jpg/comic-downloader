"""Generates and serves a small local website that mimics comic readers, web novels and ebook pages.

Used by tests/test_local.py because many real sites can't be reached from CI sandboxes.
"""
from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

from PIL import Image, ImageDraw

LAZY_JS = """
<script>
const io = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { const i = e.target; i.src = i.dataset.src; io.unobserve(i); }
});
document.querySelectorAll('img[data-src]').forEach((i) => io.observe(i));
// Infinite-scroll style: the last page is only added once the reader hits the bottom.
let added = false;
window.addEventListener('scroll', () => {
  if (!added && window.innerHeight + window.scrollY >= document.body.scrollHeight - 10) {
    added = true;
    setTimeout(() => { const i = document.createElement('img'); i.src = '/img/c1_p5.avif';
      i.style = 'width:800px;height:1200px;display:block'; document.getElementById('reader').appendChild(i); }, 300);
  }
});
</script>
"""
PLACEHOLDER = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
PAGE_STYLE = "style='width:800px;height:1200px;display:block'"


def _image(path: Path, size: tuple[int, int], label: str, fmt: str | None = None, color=(240, 230, 210)) -> None:
    im = Image.new("RGB", size, color)
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, size[0] - 10, size[1] - 10], outline=(0, 0, 0), width=6)
    d.text((40, 40), label, fill=(0, 0, 0))
    for y in range(80, size[1] - 40, 90):  # make every page's bytes unique
        d.line([(30, y), (size[0] - 30, y + hash(label) % 50)], fill=(80, 80, 80), width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, fmt) if fmt else im.save(path)


def _html(title: str, body: str, head: str = "") -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title>{head}</head>"
            f"<body>{body}</body></html>")


NOVEL_PARAS = [
    "The rain had not stopped for three days, and the river had begun to climb the stone steps of the old mill.",
    "Mara watched from the window, counting the boats that drifted loose from their moorings and vanished downstream.",
    "Her grandfather said the river remembered every flood it had ever made, and that it was only ever practising.",
    "By the fourth morning the bridge was gone, and with it the only road that led back to the city and its lights.",
]


def build_site(root: Path) -> Path:
    img = root / "img"
    # --- comic chapter 1 (lazy, picture, background, srcset, duplicate, avif on infinite scroll)
    _image(img / "c1_p1.jpg", (800, 1200), "Ch1 page 1")
    (img / "c1_p1_copy.jpg").write_bytes((img / "c1_p1.jpg").read_bytes())
    _image(img / "c1_p2.webp", (800, 1200), "Ch1 page 2 webp", "WEBP")
    _image(img / "c1_p2.jpg", (800, 1200), "Ch1 page 2 jpg")
    _image(img / "c1_p3.png", (800, 1200), "Ch1 page 3 bg")
    _image(img / "c1_p4_small.jpg", (400, 600), "Ch1 page 4 small")
    _image(img / "c1_p4.jpg", (1000, 1500), "Ch1 page 4 large")
    _image(img / "c1_p5.avif", (800, 1200), "Ch1 page 5 avif", "AVIF")
    _image(img / "logo.png", (180, 60), "LOGO")
    _image(img / "thumb.jpg", (150, 220), "thumb")
    for ch, n in ((2, 2), (3, 1)):
        for p in range(1, n + 1):
            _image(img / f"c{ch}_p{p}.jpg", (800, 1200), f"Ch{ch} page {p}")
    for p in range(1, 5):
        _image(img / f"scan_{p}.jpg", (900, 1300), f"Scanned book page {p}", color=(255, 255, 250))
    _image(img / "map.png", (640, 480), "Map of the valley")

    nav = "<header><img src='/img/logo.png' alt='logo'><a href='/'>Home</a></header>"
    ad = "<div class='ad'><img src='https://ads.doubleclick.net/banner.jpg' width=728 height=90></div>"
    comic = root / "comic"
    comic.mkdir(parents=True, exist_ok=True)
    ch1 = f"""{nav}{ad}<h1>Space Cadet Comics #1</h1><div id='reader'>
      <img src='{PLACEHOLDER}' data-src='/img/c1_p1.jpg' {PAGE_STYLE}>
      <picture><source type='image/webp' srcset='/img/c1_p2.webp'><img src='/img/c1_p2.jpg' {PAGE_STYLE}></picture>
      <div style="background-image:url('/img/c1_p3.png');width:800px;height:1200px;background-size:cover"></div>
      <img src='/img/c1_p4_small.jpg' srcset='/img/c1_p4_small.jpg 400w, /img/c1_p4.jpg 1000w' {PAGE_STYLE}>
      <img src='{PLACEHOLDER}' data-src='/img/c1_p1_copy.jpg' {PAGE_STYLE}>
      </div><aside><img src='/img/thumb.jpg'> Related</aside>
      <nav><a href='/'>Prev</a> <a href='ch2.html'>Next Chapter ›</a></nav>{LAZY_JS}"""
    (comic / "ch1.html").write_text(_html("Space Cadet Comics Chapter 1 - MockComics", ch1), encoding="utf-8")
    ch2 = f"""{nav}<div id='reader'><img src='/img/c2_p1.jpg' {PAGE_STYLE}><img src='/img/c2_p2.jpg' {PAGE_STYLE}></div>
      <a href='ch1.html'>«</a> <a href='ch3.html' class='nav'>»</a>"""
    (comic / "ch2.html").write_text(_html("Space Cadet Comics Chapter 2 - MockComics", ch2), encoding="utf-8")
    ch3 = f"""{nav}<div id='reader'><img src='/img/c3_p1.jpg' {PAGE_STYLE}></div><a href='ch2.html'>Previous chapter</a>"""
    (comic / "ch3.html").write_text(_html("Space Cadet Comics Chapter 3 - MockComics", ch3), encoding="utf-8")

    # --- web novel (text chapters)
    novel = root / "novel"
    novel.mkdir(parents=True, exist_ok=True)
    meta = ("<meta name='author' content='A. Writer'><meta property='og:site_name' content='MockNovels'>"
            "<meta property='og:novel:book_name' content='The Flooded Valley'>")
    for n in (1, 2):
        paras = "".join(f"<p>{p} ({n}.{i})</p>" for i in range(6) for p in NOVEL_PARAS)
        image = "<figure><img src='/img/map.png' alt='map'></figure>" if n == 1 else ""
        nxt = "<a href='2.html'>Next Chapter →</a>" if n == 1 else "<a href='1.html'>← Previous</a>"
        body = (f"{nav}<div class='sidebar'><ul><li><a href='/'>Popular</a></li></ul></div>"
                f"<article><h1>Chapter {n}: {'The Rain' if n == 1 else 'The Bridge'}</h1>"
                f"<h2>Part one</h2>{paras[:len(paras) // 2]}{image}<h2>Part two</h2>{paras[len(paras) // 2:]}</article>"
                f"<div class='nav'>{nxt}</div><footer>© MockNovels</footer>")
        (novel / f"{n}.html").write_text(_html(f"The Flooded Valley - Chapter {n} | MockNovels", body, meta),
                                         encoding="utf-8")

    # --- ebook files, embedded viewer, link page, scanned book
    files = root / "files"
    files.mkdir(parents=True, exist_ok=True)
    import img2pdf
    (files / "book.pdf").write_bytes(img2pdf.convert([str(img / "scan_1.jpg"), str(img / "scan_2.jpg")]))
    (files / "direct.pdf").write_bytes((files / "book.pdf").read_bytes())
    _make_epub(files / "book.epub")
    book = root / "book"
    (root / "pdfjs").mkdir(parents=True, exist_ok=True)
    (root / "pdfjs" / "viewer.html").write_text(_html("PDF viewer", "<div id='viewer'>pdf.js</div>"), encoding="utf-8")
    book.mkdir(parents=True, exist_ok=True)
    (book / "viewer.html").write_text(_html("Old Book - Online Reader", f"{nav}<iframe src='/pdfjs/viewer.html?file=%2Ffiles%2Fbook.pdf' width=800 height=1000></iframe>"), encoding="utf-8")
    (book / "links.html").write_text(_html("Old Book - details", f"{nav}<h1>Old Book</h1><p>A public-domain book.</p>"
                                           "<a href='/files/book.epub'>Download EPUB</a> <a href='/read'>Read online</a>"), encoding="utf-8")
    scan = "".join(f"<img src='/img/scan_{p}.jpg' style='width:900px;height:1300px;display:block'>" for p in range(1, 5))
    (book / "scan.html").write_text(_html("Scanned Book - page view", f"{nav}<div>{scan}</div>"), encoding="utf-8")
    (root / "index.html").write_text(_html("Mock site", "<a href='/comic/ch1.html'>comic</a>"), encoding="utf-8")
    return root


def _make_epub(path: Path) -> None:
    from ebooklib import epub

    b = epub.EpubBook()
    b.set_identifier("mock-book")
    b.set_title("Old Book")
    b.set_language("en")
    c = epub.EpubHtml(title="One", file_name="one.xhtml", lang="en")
    c.content = "<h1>One</h1><p>Hello.</p>"
    b.add_item(c)
    b.toc = (c,)
    b.add_item(epub.EpubNcx())
    b.add_item(epub.EpubNav())
    b.spine = ["nav", c]
    epub.write_epub(str(path), b)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".avif": "image/avif", ".webp": "image/webp", ".epub": "application/epub+zip"}

    def log_message(self, *args) -> None:
        pass


def serve(root: Path, port: int = 0) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    site = build_site(Path(__file__).parent / "output" / "site")
    srv, base = serve(site, 8765)
    print(f"Mock site at {base}/comic/ch1.html  (Ctrl+C to stop)")
    threading.Event().wait()
