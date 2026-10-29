@echo off
rem Builds dist\ComicDownloader.exe (single file) with flet pack (a PyInstaller wrapper that bundles the Flet client).
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && py -3.11 -c "" >nul 2>nul && set "PY=py -3.11"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :error
)
call ".venv\Scripts\activate.bat" || goto :error

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements-build.txt || goto :error

rem Do not bundle a browser into the exe - Chromium is downloaded next to the exe on first run.
set "PLAYWRIGHT_BROWSERS_PATH="

set "ICON_ARGS="
if exist "assets\icon.ico" set "ICON_ARGS=--icon assets\icon.ico"

flet pack app.py --name ComicDownloader %ICON_ARGS% ^
    --product-name "Comic Downloader" --file-description "Comic Downloader" ^
    --product-version 1.0.0 --file-version 1.0.0.0 --company-name "Comic Downloader" ^
    --yes -- ^
    --collect-data=trafilatura --collect-data=justext --collect-data=htmldate ^
    --collect-data=courlan --collect-data=dateparser --collect-data=tld --collect-data=tldextract ^
    --collect-submodules=ebooklib --exclude-module=tests || goto :error

echo.
echo Done: %CD%\dist\ComicDownloader.exe
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
