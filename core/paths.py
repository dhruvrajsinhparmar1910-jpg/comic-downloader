"""Filesystem locations used by the app (works both from source and as a PyInstaller exe)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ComicDownloader"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def install_dir() -> Path:
    """Folder that contains the exe (or the project root when run from source)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Folder for settings and the browser. Next to the exe if writable, else per-user data dir."""
    base = install_dir()
    if _writable(base):
        return base
    fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def settings_file() -> Path:
    return app_dir() / "settings.json"


def browsers_dir() -> Path:
    return app_dir() / "ms-playwright"


def default_output_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return (downloads if downloads.is_dir() else Path.home()) / APP_NAME


def configure_playwright_env() -> None:
    """Point Playwright at the app-local browser folder. Must run before Playwright starts."""
    if is_frozen() or "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir())
