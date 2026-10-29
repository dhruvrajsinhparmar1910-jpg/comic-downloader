"""Comic Downloader entry point (also the PyInstaller script)."""
from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import paths  # noqa: E402

paths.configure_playwright_env()  # before Playwright is imported anywhere


def run() -> None:
    import flet as ft

    from ui.app import main

    ft.run(main)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
