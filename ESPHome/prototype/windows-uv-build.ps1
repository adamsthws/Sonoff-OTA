# Build ESPHome/sonoff_s60.yaml on Windows with a portable uv, keeping everything
# it downloads under one short folder. In the emulator's Windows:
#     & C:\S60\repo\ESPHome\prototype\windows-uv-build.ps1
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest crawls with its progress bar
$started = Get-Date

# Short: ESP-IDF's folders are deep, and Windows' path limit is 260 characters.
$root = "C:\S60\b"
$uvVersion = "0.13.0"
$uvSha256 = "088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c"
New-Item -ItemType Directory -Force $root, "$root\cfg" | Out-Null

Write-Host "Downloading uv $uvVersion..."
$zip = "$root\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $uvSha256) { throw "uv download does not match its SHA-256" }
Expand-Archive $zip "$root\uv" -Force

# Everything uv and ESPHome keep, in $root; no Python but uv's own.
$env:UV_CACHE_DIR = "$root\uv-cache"
$env:UV_PYTHON_INSTALL_DIR = "$root\python"
$env:UV_PYTHON_BIN_DIR = "$root\bin"
$env:UV_TOOL_DIR = "$root\tools"
$env:UV_TOOL_BIN_DIR = "$root\bin"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$env:UV_NO_CONFIG = "1"
$env:ESPHOME_ESP_IDF_PREFIX = "$root\idf"

$esphome = Split-Path $PSScriptRoot  # the config is in ESPHome\, beside this folder
Copy-Item "$esphome\sonoff_s60.yaml" "$root\cfg"
Set-Location "$root\cfg"
Write-Host "Building ESPHome (the first time downloads ESP-IDF, about 3 GB)..."
# Windows PowerShell turns a native program's stderr into errors: let it run on.
$ErrorActionPreference = "Continue"
& "$root\uv\uv.exe" tool run --python 3.12 --from esphome==2026.9.1 esphome compile sonoff_s60.yaml 2>&1 |
    ForEach-Object { "$_" } | Tee-Object "$root\build.log"
$code = $LASTEXITCODE

$image = "$root\cfg\.esphome\build\sonoff-s60\build\firmware.ota.bin"
Write-Host ""
Write-Host ("Exit code {0}, after {1:N0} minutes" -f $code, ((Get-Date) - $started).TotalMinutes)
if (Test-Path $image) {
    Write-Host ("Built: {0} ({1:N0} bytes)" -f $image, (Get-Item $image).Length)
} else {
    Write-Host "No image was built; the log is $root\build.log"
}
