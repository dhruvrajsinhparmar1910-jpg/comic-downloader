# Comic Downloader

Windows desktop app that downloads comics, manga and books from (almost) any website.
Paste a chapter, series or book URL. The app opens it in headless Chromium (Playwright), finds the
pages in reading order and saves them as **CBZ**, **PDF**, **EPUB** or a plain **images folder**.

## Features

- **Comic mode:** scrolls slowly to trigger lazy-loading, then waits for the network to go idle. Images are
  collected from `<img>` (`src`, `data-src`, `data-lazy-src`, `srcset`, …), `<picture>`, SVG `<image>` and
  CSS `background-image`. If the DOM yields nothing, it falls back to the image responses seen on the network.
  - Skips ads, icons and logos: anything smaller than 300 px on either side, plus known ad and tracker domains.
  - Keeps DOM order and removes duplicates by URL and by file hash.
  - Downloads with the page's cookies, `Referer` and `User-Agent`, retrying 3 times.
- **Ebook mode** detects the page type automatically:
  - **Direct PDF/EPUB link, embedded viewer (pdf.js, iframe, `?file=`) or download link:** downloads the
    original file as-is.
  - **Image-based book pages:** uses the comic capture flow and saves a PDF.
  - **Text chapters (web novels, online books):** extracts the main content with trafilatura and keeps headings,
    paragraphs and inline images. The result is an EPUB (or PDF) with title and author taken from the page
    metadata, plus a table of contents.
  - **DRM readers (Kindle Cloud Reader, Google Play Books, Kobo, Apple Books):** skipped, with a clear message.
- **Auto mode:** picks comic or ebook handling for each URL.
- **Whole series:** follows the "Next chapter" link (`next`, `next chapter`, `›`, `»`, `→`, `rel="next"`) until
  there isn't one, or until it reaches the max-chapters limit. Comic chapters are saved as `Chapter 001`,
  `Chapter 002`, …. Text chapters are combined into one book.
- **Exports:** CBZ (`001.jpg`, `002.jpg`, …), PDF (img2pdf), EPUB (ebooklib) or a folder. WebP, AVIF and GIF
  images are converted to JPG with Pillow.
- **Use my Chrome profile:** runs with a persistent browser profile, for sites that need you to log in.
- **Polite by default:** waits a random 1–2 s between pages and processes one chapter at a time.
- **Settings** are saved to `settings.json` next to the exe.

## Using the app

1. Paste a URL. You can paste several URLs at once, separated by spaces or new lines. Click **Add to queue**.
2. Pick **Mode** (Auto / Comic / Ebook) and **Format** (CBZ / PDF / EPUB / Images folder). Turn on
   **Whole series** to follow "Next chapter" links. The current choices are stored with each queued item.
3. Choose the output folder and click **Start**. Each item shows its own progress bar, and the log panel
   shows the details. **Stop** finishes the current step and then stops.
4. Use the retry button to re-run failed items, and the folder button to open the result.

**First run:** if the browser is missing, the app downloads Chromium (about 150 MB) into `ms-playwright\`
next to the exe and shows a progress banner. This happens only once.

### Output layout

| What | Result |
| --- | --- |
| Single comic chapter | `<Output>\<Page title>.cbz` (or `.pdf`, or a folder `<Page title>\001.jpg …`) |
| Comic series | `<Output>\<Series>\Chapter 001.cbz`, `Chapter 002.cbz`, … |
| Text book / web novel | `<Output>\<Book title>.epub` (or `.pdf`), one TOC entry per chapter |
| Original PDF/EPUB | `<Output>\<original file name>` |

Format fallbacks: text pages chosen with CBZ or Images save as EPUB, and image pages chosen with EPUB save as PDF.

### Sites that need a login

1. Open **Advanced** and choose a profile folder. The default is `browser-profile\` next to the exe.
2. Paste the site URL and click **Log in…**. A visible browser window opens. Log in, then close the window.
3. Turn on **Use my Chrome profile**. Downloads now run with that session.

To use your installed Chrome or Edge instead of the built-in Chromium, change **Browser** in **Advanced**.
If you point the profile folder at your real Chrome profile, close Chrome first, because a profile can only
be used by one browser at a time. A dedicated profile folder is more reliable.

### Advanced settings (`settings.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `delay_min` / `delay_max` | 1.0 / 2.0 | Random wait between pages, in seconds |
| `max_chapters` | 50 | Series limit |
| `min_image_size` | 300 | Smaller images (either side, in px) are treated as icons or ads |
| `page_timeout` | 60 | Navigation timeout, in seconds |
| `browser_channel` | `chromium` | `chromium`, `chrome` or `msedge` |
| `show_browser` | false | Run the browser visibly (useful for debugging) |
| `use_profile` / `profile_dir` | false / `browser-profile` | Persistent login profile |

## Running from source

Requires Python 3.11 on Windows. macOS and Linux also work for development.

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Chromium downloads automatically on first start. To fetch it ahead of time:

```bat
python app.py --cli --install-browser
```

There is also a headless command-line mode, mainly for scripting and debugging:

```bat
python app.py --cli https://example.com/chapter-1 --series --format cbz --out D:\Comics
```

Set `CD_DEBUG=1` to print full tracebacks in the log.

## Building the .exe

```bat
build.bat
```

This creates `.venv`, installs `requirements-build.txt` and runs `flet pack`. `flet pack` is Flet's
PyInstaller wrapper, which also bundles the Flet desktop client. The result is a single file:
**`dist\ComicDownloader.exe`**.

- Chromium is not bundled; it is downloaded next to the exe on first run. Ship only the exe.
- The exe unpacks itself to a temp folder at startup, so the first window takes a few seconds to appear.
- If the folder containing the exe isn't writable (for example, Program Files), settings and the browser
  go to `%LOCALAPPDATA%\ComicDownloader` instead.
- To add an icon, put an `assets\icon.ico` file in the project and `build.bat` will use it.

## Project layout

```
app.py              entry point (UI, or --cli)
core/paths.py       app folder, settings file, PLAYWRIGHT_BROWSERS_PATH
core/settings.py    JSON settings
core/browser.py     Playwright session, persistent profile, first-run Chromium install
core/scraper.py     navigation, auto-scroll, image collection, ad filter, download/retry/dedupe
core/series.py      "Next chapter" detection and chapter walking
core/ebook.py       page-type detection, PDF/EPUB discovery, DRM check, text extraction
core/exporter.py    CBZ / PDF / folder / EPUB / text-PDF export
core/engine.py      queue runner that ties it all together
ui/app.py           Flet UI
tests/              local mock site + end-to-end tests
build.bat           builds dist\ComicDownloader.exe
```

## Tests

```bat
pip install pytest
python -m pytest tests -v
```

The tests start a local mock website covering these cases:

- Lazy images, `<picture>`, CSS backgrounds, `srcset`, infinite scroll, duplicates, ads and logos.
- "Next chapter" links using text and using `»`.
- A web novel with metadata.
- A pdf.js-style embedded viewer, an EPUB download link and a direct PDF URL.
- Scanned image pages.
- A DRM URL.

Each case runs the full engine and checks the CBZ, PDF and EPUB outputs.

To test by hand in the app, run `python tests/mock_site.py` and paste `http://127.0.0.1:8765/comic/ch1.html`.

## Limitations

- **Paginated readers:** readers that show one page per URL and only have a "next page" button are treated
  as chapters by series mode. Readers that draw pages on `<canvas>` can only be captured through the network
  fallback.
- **Detection:** the next-chapter link and the main content are found heuristically. Unusual layouts may need
  Comic or Ebook mode chosen manually.
- **Legal:** only download content you are allowed to (public domain, free, or content you own). DRM is never
  bypassed.
