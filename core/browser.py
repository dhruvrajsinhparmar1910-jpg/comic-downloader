"""Playwright browser lifecycle and first-run Chromium installation."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .settings import Settings

LogFn = Callable[[str], None]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{version} Safari/537.36"
)
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


class BrowserMissingError(RuntimeError):
    pass


# --------------------------------------------------------------------------- install

def _driver_command() -> list[str]:
    from playwright._impl._driver import compute_driver_executable

    driver = compute_driver_executable()
    return list(driver) if isinstance(driver, (tuple, list)) else [str(driver)]


def _driver_env() -> dict:
    from playwright._impl._driver import get_driver_env

    env = get_driver_env()
    env.update({k: v for k, v in os.environ.items() if k not in env})
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    return env


def chromium_installed() -> bool:
    """True if the Chromium build matching this Playwright version is on disk."""
    if os.environ.get("CD_CHROMIUM_EXECUTABLE"):
        return True
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


def install_chromium(log: LogFn, progress: Optional[Callable[[float], None]] = None) -> bool:
    """Download Chromium into PLAYWRIGHT_BROWSERS_PATH, streaming progress to the UI."""
    cmd = _driver_command() + ["install", "--no-shell", "chromium"]
    log(f"Installing Chromium into {os.environ.get('PLAYWRIGHT_BROWSERS_PATH')} (one-time, ~150 MB)...")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_driver_env(),
            creationflags=flags,
        )
    except OSError as e:
        log(f"Could not start the Playwright installer: {e}")
        return False
    last_pct = -1
    buf = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        *lines, buf = re.split(rb"[\r\n]", buf)
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            m = re.search(r"(\d{1,3})%", line)
            if m:
                pct = int(m.group(1))
                if progress:
                    progress(pct / 100)
                if pct // 10 != last_pct // 10:
                    log(f"  download {pct}%")
                last_pct = pct
            else:
                log("  " + line)
    code = proc.wait()
    if code != 0:
        log(f"Chromium install failed (exit code {code}).")
        return False
    log("Chromium installed.")
    return True


def ensure_chromium(log: LogFn, progress: Optional[Callable[[float], None]] = None) -> bool:
    if chromium_installed():
        return True
    return install_chromium(log, progress)


# --------------------------------------------------------------------------- session

class BrowserSession:
    """One Playwright instance + context, used from a single worker thread."""

    def __init__(self, settings: Settings, log: LogFn):
        self.settings = settings
        self.log = log
        self._pw = None
        self._browser = None
        self.context = None
        self._pdf_browser = None
        self._headless = True
        self.user_agent = DEFAULT_UA.format(version="131.0.0.0")

    def _launch_kwargs(self, headless: bool) -> dict:
        kw: dict = {"headless": headless, "args": LAUNCH_ARGS}
        exe = os.environ.get("CD_CHROMIUM_EXECUTABLE")
        if exe:
            kw["executable_path"] = exe
        else:
            # "chromium" channel = full Chromium with new headless mode (no separate headless shell).
            kw["channel"] = self.settings.browser_channel or "chromium"
        return kw

    def start(self, headless: Optional[bool] = None) -> "BrowserSession":
        from playwright.sync_api import Error as PwError, sync_playwright

        if headless is None:
            headless = not self.settings.show_browser
        self._headless = headless
        self._pw = sync_playwright().start()
        kw = self._launch_kwargs(headless)
        ctx_kw = {"viewport": {"width": 1280, "height": 900}, "accept_downloads": True}
        try:
            if self.settings.use_profile:
                profile = Path(self.settings.profile_dir)
                profile.mkdir(parents=True, exist_ok=True)
                self.context = self._pw.chromium.launch_persistent_context(
                    str(profile), user_agent=self.user_agent, **kw, **ctx_kw
                )
            else:
                self._browser = self._pw.chromium.launch(**kw)
                version = self._browser.version.split(" ")[0] or "131.0.0.0"
                self.user_agent = DEFAULT_UA.format(version=version)
                self.context = self._browser.new_context(user_agent=self.user_agent, **ctx_kw)
        except PwError as e:
            msg = str(e)
            self.close()
            if "Executable doesn't exist" in msg or "is not found at" in msg:
                raise BrowserMissingError(msg) from e
            if "ProcessSingleton" in msg or ("profile" in msg.lower() and "in use" in msg.lower()):
                raise RuntimeError(
                    "The browser profile is in use. Close Chrome (or the login window) and try again."
                ) from e
            raise
        self.context.set_default_timeout(self.settings.page_timeout * 1000)
        self.context.set_default_navigation_timeout(self.settings.page_timeout * 1000)
        return self

    def new_page(self):
        return self.context.new_page()

    def headless_page(self):
        """A page in a headless browser (page.pdf() only works headless)."""
        if not self._headless:
            if self._pdf_browser is None:
                self._pdf_browser = self._pw.chromium.launch(**self._launch_kwargs(True))
            return self._pdf_browser.new_page()
        return self.new_page()

    def close(self) -> None:
        for obj in (self.context, self._browser, self._pdf_browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self.context = self._browser = self._pdf_browser = self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def start_session(settings: Settings, log: LogFn, headless: Optional[bool] = None) -> BrowserSession:
    """Start a session, installing Chromium first if it's missing."""
    try:
        return BrowserSession(settings, log).start(headless)
    except BrowserMissingError:
        if settings.browser_channel != "chromium":
            raise RuntimeError(
                f"Browser channel '{settings.browser_channel}' is not installed on this PC. "
                "Install it or switch the browser to 'chromium' in Advanced settings."
            )
        if not install_chromium(log):
            raise
        return BrowserSession(settings, log).start(headless)
