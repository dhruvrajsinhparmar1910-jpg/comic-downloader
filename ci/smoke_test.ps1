# Real-site smoke test for the built exe: downloads one public-domain comic from each site
# and fails if a site yields no saved images.
# A site that only serves a bot check (e.g. Cloudflare "Just a moment...", which blocks
# datacenter IPs like CI runners) is reported as skipped with a warning instead.
param(
    [string]$Exe = "dist\ComicDownloader.exe",
    [string]$WorkDir = "smoke"
)
$ErrorActionPreference = "Stop"
$env:CD_DEBUG = "1"
$Exe = (Resolve-Path $Exe).Path
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$WorkDir = (Resolve-Path $WorkDir).Path
$UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

function Invoke-Exe([string[]]$ArgList, [string]$Log) {
    $quoted = $ArgList | ForEach-Object { '"' + $_ + '"' }
    $p = Start-Process -FilePath $Exe -ArgumentList ($quoted + @('--log', "`"$Log`"")) -Wait -PassThru -NoNewWindow
    if (Test-Path $Log) { Get-Content $Log -Encoding utf8 | ForEach-Object { Write-Host "    $_" } }
    return $p.ExitCode
}

# Pull candidate URLs out of an HTML page (best effort: the page may be behind bot protection).
function Find-Links([string]$PageUrl, [string]$Pattern, [string]$Base) {
    try {
        $html = (Invoke-WebRequest -Uri $PageUrl -UserAgent $UA -UseBasicParsing -TimeoutSec 60).Content
    } catch {
        Write-Host "  could not fetch ${PageUrl}: $($_.Exception.Message)"
        return @()
    }
    $found = [regex]::Matches($html, $Pattern) | ForEach-Object { $_.Value -replace '&amp;', '&' } |
        Select-Object -Unique
    return @($found | ForEach-Object { if ($_ -match '^https?://') { $_ } else { $Base + $_.TrimStart('/') } })
}

Write-Host "== Installing Chromium (first-run path of the exe)"
$code = Invoke-Exe @('--cli', '--install-browser') (Join-Path $WorkDir "install.log")
if ($code -ne 0) { throw "Chromium install failed (exit $code)" }

$sites = [ordered]@{
    "comicbookplus" = @{
        Discover = @(
            @{ Page = "https://comicbookplus.com/?cid=1507"; Pattern = '(?:https?://(?:www\.)?comicbookplus\.com/)?\?dlid=\d+'; Base = "https://comicbookplus.com/" }
        )
        Fallback = @("https://comicbookplus.com/?dlid=16348")
    }
    "digitalcomicmuseum" = @{
        Discover = @(
            @{ Page = "https://digitalcomicmuseum.com/index.php?dlid=1510"; Pattern = '(?:https?://(?:www\.)?digitalcomicmuseum\.com/)?/?preview/index\.php\?did=\d+[^"''\s<>]*'; Base = "https://digitalcomicmuseum.com/" }
        )
        Fallback = @("https://digitalcomicmuseum.com/preview/index.php?did=1510&page=1",
                     "https://digitalcomicmuseum.com/index.php?dlid=1510")
    }
}

$failed = @()
$passed = @()
$skipped = @()
foreach ($name in $sites.Keys) {
    $site = $sites[$name]
    Write-Host "== $name"
    $urls = @()
    foreach ($d in $site.Discover) { $urls += Find-Links $d.Page $d.Pattern $d.Base }
    $urls = @(@($urls | Select-Object -First 3) + @($site.Fallback) | Select-Object -Unique)
    Write-Host "  candidates: $($urls -join ', ')"
    $ok = $false
    $allBlocked = $true
    $i = 0
    foreach ($url in $urls) {
        $i++
        $out = Join-Path $WorkDir "$name-$i"
        Write-Host "  -> $url"
        $code = Invoke-Exe @('--cli', $url, '--mode', 'comic', '--format', 'folder', '--show-browser', '--out', $out) (Join-Path $WorkDir "$name-$i.log")
        $images = @(Get-ChildItem -Path $out -Recurse -File -Include *.jpg, *.png -ErrorAction SilentlyContinue)
        Write-Host "  exit=$code images=$($images.Count)"
        if ($images.Count -gt 0) {
            $images | Select-Object -First 5 | ForEach-Object { Write-Host "    $($_.FullName) ($($_.Length) bytes)" }
            $ok = $true
            break
        }
        $log = Join-Path $WorkDir "$name-$i.log"
        $blocked = (Test-Path $log) -and (Select-String -Path $log -Pattern 'showing a bot check' -Quiet)
        if (-not $blocked) { $allBlocked = $false }
    }
    if ($ok) { $passed += $name }
    elseif ($allBlocked) {
        $skipped += $name
        Write-Host "::warning title=Smoke test skipped::$name served a bot check (Cloudflare) to this runner; no download was possible from CI."
    }
    else { $failed += $name }
}

Write-Host "passed: $($passed -join ', ')  skipped (bot check): $($skipped -join ', ')  failed: $($failed -join ', ')"
if ($failed.Count -gt 0) { throw "No images saved for: $($failed -join ', ')" }
Write-Host "Smoke test passed."
