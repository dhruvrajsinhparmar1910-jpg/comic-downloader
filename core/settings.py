"""User settings persisted as JSON next to the exe."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import paths

MODES = ("auto", "comic", "ebook")
FORMATS = ("cbz", "pdf", "epub", "folder")
CHANNELS = ("chromium", "chrome", "msedge")


@dataclass
class Settings:
    output_dir: str = ""
    mode: str = "auto"
    fmt: str = "cbz"
    whole_series: bool = False
    max_chapters: int = 50
    delay_min: float = 1.0
    delay_max: float = 2.0
    min_image_size: int = 300
    page_timeout: int = 60
    use_profile: bool = False
    profile_dir: str = ""
    browser_channel: str = "chromium"
    show_browser: bool = False

    def __post_init__(self) -> None:
        if not self.output_dir:
            self.output_dir = str(paths.default_output_dir())
        if not self.profile_dir:
            self.profile_dir = str(paths.app_dir() / "browser-profile")
        if self.mode not in MODES:
            self.mode = "auto"
        if self.fmt not in FORMATS:
            self.fmt = "cbz"
        if self.browser_channel not in CHANNELS:
            self.browser_channel = "chromium"
        self.max_chapters = max(1, int(self.max_chapters))
        self.min_image_size = max(0, int(self.min_image_size))
        self.delay_min = max(0.0, float(self.delay_min))
        self.delay_max = max(self.delay_min, float(self.delay_max))

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or paths.settings_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in data.items() if k in known})
        except (TypeError, ValueError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or paths.settings_file()
        try:
            path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass
