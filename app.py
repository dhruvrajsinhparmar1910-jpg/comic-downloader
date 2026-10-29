"""Comic Downloader entry point (also the PyInstaller script).

    ComicDownloader.exe                      -> desktop UI
    ComicDownloader.exe --cli URL [options]  -> headless download (see --cli --help)
"""
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import paths  # noqa: E402

paths.configure_playwright_env()  # before Playwright is imported anywhere


def run_cli(argv: list[str]) -> int:
    import argparse
    import threading

    from core import browser
    from core.engine import DONE, Engine, JobItem
    from core.settings import FORMATS, MODES, Settings

    ap = argparse.ArgumentParser(prog="ComicDownloader --cli")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--format", dest="fmt", choices=FORMATS)
    ap.add_argument("--series", action="store_true", help="follow 'Next chapter' links")
    ap.add_argument("--max-chapters", type=int)
    ap.add_argument("--out", help="output folder")
    ap.add_argument("--install-browser", action="store_true", help="only download Chromium")
    ap.add_argument("--log", help="also write the log to this file (the exe has no console)")
    args = ap.parse_args(argv)

    log_file = open(args.log, "a", encoding="utf-8") if args.log else None

    def log(msg: str) -> None:
        if sys.stdout is not None:
            print(msg, flush=True)
        if log_file is not None:
            log_file.write(msg + "\n")
            log_file.flush()

    if args.install_browser:
        return 0 if browser.ensure_chromium(log) else 1
    if not args.urls:
        ap.error("no URLs given")
    s = Settings.load()
    if args.out:
        s.output_dir = args.out
    if args.max_chapters:
        s.max_chapters = args.max_chapters
    items = [JobItem(url=u, mode=args.mode or s.mode, fmt=args.fmt or s.fmt,
                     whole_series=args.series or s.whole_series) for u in args.urls]
    Engine(s, log, stop=threading.Event()).run(items)
    return 0 if all(i.status == DONE for i in items) else 1


def run_ui() -> None:
    import flet as ft

    from ui.app import main

    ft.run(main)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--cli" in sys.argv:
        sys.exit(run_cli([a for a in sys.argv[1:] if a != "--cli"]))
    run_ui()
