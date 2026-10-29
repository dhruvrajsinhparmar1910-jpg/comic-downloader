"""End-to-end tests against the local mock site.  Run:  python -m pytest tests -v   (or python tests/test_local.py)"""
from __future__ import annotations

import shutil
import sys
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import paths  # noqa: E402

paths.configure_playwright_env()

from core.engine import DONE, ERROR, Engine, JobItem  # noqa: E402
from core.settings import Settings  # noqa: E402
from tests.mock_site import build_site, serve  # noqa: E402

OUT = ROOT / "tests" / "output"
_state: dict = {}


def setup_module(_=None):
    shutil.rmtree(OUT, ignore_errors=True)
    site = build_site(OUT / "site")
    _state["server"], _state["base"] = serve(site)


def teardown_module(_=None):
    _state["server"].shutdown()


def run(url: str, mode: str, fmt: str, series: bool = False, name: str = "x", **settings) -> tuple[JobItem, Path]:
    out = OUT / "downloads" / name
    s = Settings(**{"output_dir": str(out), "delay_min": 0.1, "delay_max": 0.2, "max_chapters": 10, **settings})
    item = JobItem(url=_state["base"] + url if url.startswith("/") else url, mode=mode, fmt=fmt, whole_series=series)
    logs: list[str] = []
    Engine(s, logs.append, stop=threading.Event()).run([item])
    print("\n".join(logs))
    return item, out


def _zip_names(p: Path) -> list[str]:
    with zipfile.ZipFile(p) as z:
        return sorted(z.namelist())


def test_comic_series_cbz():
    item, out = run("/comic/ch1.html", "comic", "cbz", series=True, name="series")
    assert item.status == DONE, item.message
    series_dir = out / "Space Cadet Comics"
    cbz = sorted(series_dir.glob("*.cbz"))
    assert [p.name for p in cbz] == ["Chapter 001.cbz", "Chapter 002.cbz", "Chapter 003.cbz"]
    # lazy img, <picture>, background, srcset (largest), infinite-scroll avif; duplicate + logo/thumb/ad dropped
    assert _zip_names(cbz[0]) == ["001.jpg", "002.jpg", "003.png", "004.jpg", "005.jpg"]
    with zipfile.ZipFile(cbz[0]) as z:
        from PIL import Image
        import io
        assert Image.open(io.BytesIO(z.read("004.jpg"))).size == (1000, 1500)  # srcset largest
    assert len(_zip_names(cbz[1])) == 2 and len(_zip_names(cbz[2])) == 1


def test_comic_single_pdf_and_folder():
    item, out = run("/comic/ch2.html", "auto", "pdf", name="single_pdf")
    assert item.status == DONE, item.message
    pdf = list(out.glob("*.pdf"))
    assert len(pdf) == 1 and pdf[0].read_bytes()[:5] == b"%PDF-"
    item, out = run("/comic/ch2.html", "comic", "folder", name="single_folder")
    assert item.status == DONE, item.message
    folder = [p for p in out.iterdir() if p.is_dir() and not p.name.startswith(".")][0]
    assert sorted(p.name for p in folder.iterdir()) == ["001.jpg", "002.jpg"]


def test_novel_series_epub():
    item, out = run("/novel/1.html", "auto", "epub", series=True, name="novel_epub")
    assert item.status == DONE, item.message
    epubs = list(out.glob("*.epub"))
    assert [p.name for p in epubs] == ["The Flooded Valley.epub"]
    from ebooklib import epub
    book = epub.read_epub(str(epubs[0]))
    assert book.get_metadata("DC", "creator")[0][0] == "A. Writer"
    titles = [t.title for t in book.toc]
    assert titles == ["Chapter 1: The Rain", "Chapter 2: The Bridge"]
    names = [i.file_name for i in book.get_items()]
    assert any(n.startswith("images/") for n in names)
    chap = book.get_item_with_href("chap_0001.xhtml").get_content().decode()
    assert "<h2>Part one</h2>" in chap and "images/c001_001.png" in chap and "Popular" not in chap


def test_novel_pdf():
    item, out = run("/novel/1.html", "ebook", "pdf", name="novel_pdf")
    assert item.status == DONE, item.message
    pdf = list(out.glob("*.pdf"))
    assert len(pdf) == 1 and pdf[0].stat().st_size > 5000


def test_ebook_files():
    item, out = run("/book/viewer.html", "ebook", "epub", name="viewer")
    assert item.status == DONE, item.message
    assert (out / "book.pdf").read_bytes()[:5] == b"%PDF-"
    item, out = run("/book/links.html", "auto", "epub", name="links")
    assert item.status == DONE, item.message
    assert (out / "book.epub").exists()
    item, out = run("/files/direct.pdf", "auto", "pdf", name="direct")
    assert item.status == DONE, item.message
    assert (out / "direct.pdf").exists()


def test_scanned_book_pdf():
    item, out = run("/book/scan.html", "ebook", "epub", name="scan")
    assert item.status == DONE, item.message
    assert len(list(out.glob("*.pdf"))) == 1


def test_max_chapters_and_profile():
    item, out = run("/comic/ch1.html", "comic", "folder", series=True, name="profile", max_chapters=2,
                    use_profile=True, profile_dir=str(OUT / "profile"))
    assert item.status == DONE, item.message
    chapters = sorted(p.name for p in (out / "Space Cadet Comics").iterdir())
    assert chapters == ["Chapter 001", "Chapter 002"]
    assert (OUT / "profile").is_dir() and any((OUT / "profile").iterdir())


def test_series_title():
    from core.series import series_title
    assert series_title("Read One Piece Chapter 1000 Online - MangaSite", "x") == "One Piece"
    assert series_title("Batman #12 | ReadComics", "x") == "Batman"
    assert series_title("", "host") == "host"


def test_drm_skipped():
    item, _ = run("https://read.amazon.com/?asin=B000000", "ebook", "epub", name="drm")
    assert item.status == ERROR and "DRM" in item.message


if __name__ == "__main__":
    setup_module()
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                fn()
                print(f"PASS {name}")
    finally:
        teardown_module()
