"""Flet desktop UI."""
from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import flet as ft

from core import browser
from core.engine import DONE, ERROR, QUEUED, RUNNING, STOPPED, Engine, JobItem, open_login_window
from core.settings import Settings

MODE_LABELS = {"auto": "Auto", "comic": "Comic", "ebook": "Ebook"}
FORMAT_LABELS = {"cbz": "CBZ", "pdf": "PDF", "epub": "EPUB", "folder": "Images folder"}
CHANNEL_LABELS = {"chromium": "Built-in Chromium", "chrome": "Google Chrome", "msedge": "Microsoft Edge"}
STATUS_COLORS = {
    QUEUED: ft.Colors.BLUE_GREY_400, RUNNING: ft.Colors.BLUE_400, DONE: ft.Colors.GREEN_500,
    ERROR: ft.Colors.RED_400, STOPPED: ft.Colors.AMBER_600,
}
MAX_LOG_LINES = 600


def open_path(path: str) -> None:
    if not path:
        return
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


class QueueRow:
    """Visual row for one JobItem."""

    def __init__(self, app: "App", item: JobItem):
        self.app = app
        self.item = item
        self.status = ft.Text(size=12, weight=ft.FontWeight.BOLD)
        self.detail = ft.Text(size=12, color=ft.Colors.ON_SURFACE_VARIANT, max_lines=2,
                              overflow=ft.TextOverflow.ELLIPSIS)
        self.bar = ft.ProgressBar(value=0, bar_height=6, border_radius=3)
        self.open_btn = ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="Open output", icon_size=18,
                                      on_click=lambda e: open_path(self.item.output))
        self.retry_btn = ft.IconButton(ft.Icons.REPLAY, tooltip="Retry", icon_size=18, on_click=self._retry)
        self.remove_btn = ft.IconButton(ft.Icons.CLOSE, tooltip="Remove", icon_size=18, on_click=self._remove)
        opts = (f"{MODE_LABELS[item.mode]} · {FORMAT_LABELS[item.fmt]}"
                + (" · whole series" if item.whole_series else ""))
        self.control = ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            content=ft.Column(spacing=4, controls=[
                ft.Row(spacing=8, controls=[
                    ft.Text(item.url, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                            selectable=True, size=13),
                    self.status, self.open_btn, self.retry_btn, self.remove_btn,
                ]),
                ft.Row(controls=[ft.Text(opts, size=11, color=ft.Colors.PRIMARY), self.detail], spacing=10),
                self.bar,
            ]),
        )
        self.refresh()

    def refresh(self) -> None:
        it = self.item
        self.status.value = it.status.upper()
        self.status.color = STATUS_COLORS.get(it.status)
        self.detail.value = it.message
        self.bar.value = it.progress if it.status == RUNNING else (1.0 if it.status == DONE else 0)
        self.bar.color = STATUS_COLORS.get(it.status)
        self.open_btn.visible = bool(it.output)
        self.retry_btn.visible = it.status in (ERROR, STOPPED)
        self.remove_btn.disabled = it.status == RUNNING

    def _retry(self, e) -> None:
        self.item.status = QUEUED
        self.item.message = ""
        self.item.progress = 0
        self.refresh()
        self.app.page.update()

    def _remove(self, e) -> None:
        self.app.remove_row(self)


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        self.settings = Settings.load()
        self.rows: list[QueueRow] = []
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.installing = False
        self.lock = threading.Lock()
        self.picker = ft.FilePicker()
        self._build()
        page.run_task(self._pump_events)
        threading.Thread(target=self._check_browser, daemon=True).start()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        p, s = self.page, self.settings
        p.title = "Comic Downloader"
        p.window.width, p.window.height = 1000, 820
        p.window.min_width, p.window.min_height = 760, 600
        p.theme_mode = ft.ThemeMode.SYSTEM
        p.padding = 16

        self.url = ft.TextField(label="Comic / manga / book URL", hint_text="https://...", expand=True,
                                on_submit=self._add, autofocus=True)
        self.mode = ft.Dropdown(label="Mode", width=130, value=s.mode,
                                options=[ft.DropdownOption(key=k, text=v) for k, v in MODE_LABELS.items()],
                                on_select=self._on_mode)
        self.fmt = ft.Dropdown(label="Format", width=170, value=s.fmt,
                               options=[ft.DropdownOption(key=k, text=v) for k, v in FORMAT_LABELS.items()],
                               on_select=self._save_settings)
        self.series = ft.Switch(label="Whole series", value=s.whole_series, on_change=self._save_settings)
        self.max_ch = ft.TextField(label="Max chapters", width=120, value=str(s.max_chapters), dense=True,
                                   input_filter=ft.NumbersOnlyInputFilter(), on_blur=self._save_settings)
        self.out_dir = ft.TextField(label="Output folder", value=s.output_dir, expand=True, dense=True,
                                    on_blur=self._save_settings)
        self.use_profile = ft.Switch(label="Use my Chrome profile (for sites that need login)",
                                     value=s.use_profile, on_change=self._save_settings)

        num = ft.InputFilter(regex_string=r"^[0-9]*\.?[0-9]*$")
        self.delay_min = ft.TextField(label="Delay min (s)", width=120, value=str(s.delay_min), dense=True,
                                      input_filter=num, on_blur=self._save_settings)
        self.delay_max = ft.TextField(label="Delay max (s)", width=120, value=str(s.delay_max), dense=True,
                                      input_filter=num, on_blur=self._save_settings)
        self.min_size = ft.TextField(label="Min image side (px)", width=150, value=str(s.min_image_size),
                                     dense=True, input_filter=ft.NumbersOnlyInputFilter(), on_blur=self._save_settings)
        self.channel = ft.Dropdown(label="Browser", width=200, value=s.browser_channel,
                                   options=[ft.DropdownOption(key=k, text=v) for k, v in CHANNEL_LABELS.items()],
                                   on_select=self._save_settings)
        self.show_browser = ft.Switch(label="Show browser window", value=s.show_browser,
                                      on_change=self._save_settings)
        self.profile_dir = ft.TextField(label="Profile folder", value=s.profile_dir, expand=True, dense=True,
                                        on_blur=self._save_settings)

        self.queue_list = ft.ListView(spacing=6, expand=True)
        self.empty_hint = ft.Text("Queue is empty - paste a URL above and click “Add to queue”.",
                                  color=ft.Colors.ON_SURFACE_VARIANT, italic=True)
        self.log_list = ft.ListView(spacing=0, expand=True, auto_scroll=True)
        self.banner_text = ft.Text("", size=12)
        self.banner_bar = ft.ProgressBar(value=None, bar_height=4)
        self.banner = ft.Container(visible=False, padding=8, border_radius=6,
                                   bgcolor=ft.Colors.SECONDARY_CONTAINER,
                                   content=ft.Column([self.banner_text, self.banner_bar], spacing=6))
        self.start_btn = ft.FilledButton("Start", icon=ft.Icons.PLAY_ARROW, on_click=self._start)
        self.stop_btn = ft.OutlinedButton("Stop", icon=ft.Icons.STOP, on_click=self._stop, disabled=True)

        advanced = ft.ExpansionTile(
            title=ft.Text("Advanced", size=14),
            controls_padding=ft.Padding.only(bottom=8),
            controls=[
                ft.Row([self.delay_min, self.delay_max, self.min_size, self.channel, self.show_browser],
                       wrap=True, spacing=12),
                ft.Row([self.profile_dir,
                        ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="Choose profile folder",
                                      on_click=self._pick_profile),
                        ft.OutlinedButton("Log in…", icon=ft.Icons.LOGIN, on_click=self._login,
                                          tooltip="Open a visible browser with this profile to log in to a site")]),
                ft.Text("Tip: use a dedicated profile folder. Chrome must be closed if you point this at your "
                        "real Chrome profile.", size=11, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
        )

        p.add(
            ft.Row([self.url, ft.FilledButton("Add to queue", icon=ft.Icons.ADD, on_click=self._add)]),
            ft.Row([self.mode, self.fmt, self.series, self.max_ch, self.use_profile], wrap=True, spacing=14,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.out_dir,
                    ft.IconButton(ft.Icons.FOLDER_OPEN, tooltip="Choose output folder", on_click=self._pick_out),
                    ft.IconButton(ft.Icons.OPEN_IN_NEW, tooltip="Open output folder",
                                  on_click=lambda e: open_path(self.out_dir.value))]),
            advanced,
            self.banner,
            ft.Row([ft.Text("Queue", size=16, weight=ft.FontWeight.BOLD), ft.Container(expand=True),
                    ft.TextButton("Clear finished", icon=ft.Icons.CLEANING_SERVICES, on_click=self._clear_done),
                    self.start_btn, self.stop_btn]),
            ft.Container(content=ft.Column([self.empty_hint, self.queue_list], expand=True), expand=3),
            ft.Text("Log", size=16, weight=ft.FontWeight.BOLD),
            ft.Container(content=self.log_list, expand=2, padding=8, border_radius=8,
                         bgcolor=ft.Colors.SURFACE_CONTAINER_LOW),
        )

    # ------------------------------------------------------------------ settings

    def _on_mode(self, e) -> None:
        if self.mode.value == "ebook" and self.fmt.value == "cbz":
            self.fmt.value = "epub"
        elif self.mode.value == "comic" and self.fmt.value == "epub":
            self.fmt.value = "cbz"
        self._save_settings()

    def _save_settings(self, e=None) -> None:
        s = self.settings

        def num(field: ft.TextField, default, cast):
            try:
                return cast(field.value)
            except (TypeError, ValueError):
                return default

        s.mode = self.mode.value or "auto"
        s.fmt = self.fmt.value or "cbz"
        s.whole_series = bool(self.series.value)
        s.max_chapters = num(self.max_ch, s.max_chapters, int)
        s.output_dir = (self.out_dir.value or "").strip() or s.output_dir
        s.use_profile = bool(self.use_profile.value)
        s.delay_min = num(self.delay_min, s.delay_min, float)
        s.delay_max = num(self.delay_max, s.delay_max, float)
        s.min_image_size = num(self.min_size, s.min_image_size, int)
        s.browser_channel = self.channel.value or "chromium"
        s.show_browser = bool(self.show_browser.value)
        s.profile_dir = (self.profile_dir.value or "").strip() or s.profile_dir
        s.__post_init__()  # clamp values
        s.save()
        self.page.update()

    async def _pick_out(self, e) -> None:
        path = await self.picker.get_directory_path("Choose output folder", self.out_dir.value or None)
        if path:
            self.out_dir.value = path
            self._save_settings()

    async def _pick_profile(self, e) -> None:
        path = await self.picker.get_directory_path("Choose browser profile folder", None)
        if path:
            self.profile_dir.value = path
            self._save_settings()

    # ------------------------------------------------------------------ queue

    def _add(self, e) -> None:
        urls = [u.strip() for u in (self.url.value or "").split() if u.strip()]
        if not urls:
            return
        self._save_settings()
        s = self.settings
        for url in urls:
            if not url.lower().startswith(("http://", "https://")):
                url = "https://" + url
            row = QueueRow(self, JobItem(url=url, mode=s.mode, fmt=s.fmt, whole_series=s.whole_series))
            with self.lock:
                self.rows.append(row)
            self.queue_list.controls.append(row.control)
        self.url.value = ""
        self._refresh_queue()

    def remove_row(self, row: QueueRow) -> None:
        if row.item.status == RUNNING:
            return
        with self.lock:
            if row in self.rows:
                self.rows.remove(row)
                row.item.status = "removed"
        if row.control in self.queue_list.controls:
            self.queue_list.controls.remove(row.control)
        self._refresh_queue()

    def _clear_done(self, e) -> None:
        for row in [r for r in self.rows if r.item.status == DONE]:
            self.remove_row(row)

    def _refresh_queue(self) -> None:
        self.empty_hint.visible = not self.rows
        self.page.update()

    def _next_item(self) -> Optional[JobItem]:
        with self.lock:
            for row in self.rows:
                if row.item.status == QUEUED:
                    return row.item
        return None

    # ------------------------------------------------------------------ run

    def _start(self, e) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.installing:
            self.log("Please wait until the browser download has finished.")
            return
        self._save_settings()
        if not self._next_item():
            self.log("Nothing queued.")
            return
        self.stop_event = threading.Event()
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_worker, daemon=True)
        self.worker.start()

    def _run_worker(self) -> None:
        engine = Engine(self.settings, self.log, on_progress=lambda item: self.events.put(("item", item)),
                        stop=self.stop_event)
        try:
            engine.run(self._next_item)
        except Exception as ex:  # browser failed to start, etc.
            self.log(f"ERROR: {ex}")
            with self.lock:
                for row in self.rows:
                    if row.item.status == RUNNING:
                        row.item.status = ERROR
                        row.item.message = str(ex).splitlines()[0][:120]
                        self.events.put(("item", row.item))
        finally:
            self.events.put(("finished",))

    def _stop(self, e) -> None:
        self.stop_event.set()
        self.log("Stopping after the current step…")

    def _set_running(self, running: bool) -> None:
        self.start_btn.disabled = running
        self.stop_btn.disabled = not running
        self.page.update()

    async def _login(self, e) -> None:
        if self.worker and self.worker.is_alive():
            self.log("Stop the queue before opening the login window.")
            return
        self._save_settings()
        url = (self.url.value or "").strip() or (self.rows[0].item.url if self.rows else "")
        self.worker = threading.Thread(target=self._login_worker, args=(url,), daemon=True)
        self.worker.start()

    def _login_worker(self, url: str) -> None:
        try:
            open_login_window(self.settings, url, self.log)
        except Exception as ex:
            self.log(f"ERROR: login window: {ex}")

    # ------------------------------------------------------------------ first-run browser

    def _check_browser(self) -> None:
        if browser.chromium_installed():
            return
        self.installing = True
        self.events.put(("banner", "Downloading the Chromium browser (first run only)…", None))
        ok = browser.install_chromium(
            self.log, lambda f: self.events.put(("banner", f"Downloading Chromium… {int(f * 100)}%", f)))
        self.installing = False
        self.events.put(("banner", None if ok else
                         "Chromium download failed - check your internet connection and restart the app.", 0))

    # ------------------------------------------------------------------ thread -> UI

    def log(self, msg: str) -> None:
        self.events.put(("log", f"{time.strftime('%H:%M:%S')}  {msg}"))

    async def _pump_events(self) -> None:
        """All UI mutations from worker threads happen here, on Flet's event loop."""
        while True:
            dirty = False
            try:
                while True:
                    ev = self.events.get_nowait()
                    dirty = True
                    kind = ev[0]
                    if kind == "log":
                        self.log_list.controls.append(
                            ft.Text(ev[1], size=12, font_family="Consolas", selectable=True))
                        if len(self.log_list.controls) > MAX_LOG_LINES:
                            del self.log_list.controls[:100]
                    elif kind == "item":
                        for row in self.rows:
                            if row.item is ev[1]:
                                row.refresh()
                    elif kind == "finished":
                        for row in self.rows:
                            row.refresh()
                        self._set_running(False)
                    elif kind == "banner":
                        _, text, frac = ev
                        self.banner.visible = text is not None
                        self.banner_text.value = text or ""
                        self.banner_bar.value = frac
            except queue.Empty:
                pass
            if dirty:
                try:
                    self.page.update()
                except Exception:
                    return  # window closed
            await asyncio.sleep(0.15)


def main(page: ft.Page) -> None:
    App(page)
