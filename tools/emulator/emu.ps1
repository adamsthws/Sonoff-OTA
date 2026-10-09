<#
The emulator with Windows as the computer: run the conversion tool on Windows
against the emulated S60 (see docs/emulator.md, "From Windows").

    tools\emulator\emu.ps1 -Cloud     plug and tool online (real plug must be unplugged)

Options as emu.sh's: -Cloud -Fresh -Pcap -Rebuild -NoFirewall, and -Distro to
pick the WSL distribution (its default otherwise).

Opens the emulator (emu.sh --windows, under WSL) in a window of its own - the
plug's power and serial log are there - and the link that puts Windows on its
home network in another. This window is left for the conversion:

    .venv\Scripts\python tools\s60_autoflash.py run

Needs, once: WSL with Docker, and tools\emulator\windows-setup.ps1.
#>
param([switch]$Cloud, [switch]$Fresh, [switch]$Pcap, [switch]$Rebuild, [switch]$NoFirewall,
      [string]$Distro = "")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Adapter = "S60 emulator"

if (-not (Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue)) {
    throw "No '$Adapter' network adapter: run tools\emulator\windows-setup.ps1 once first."
}

# Windows' own Python, for the link and the conversion tool.
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Setting up .venv for Windows' Python..."
    py -3 -m venv (Join-Path $Root ".venv")
    & $Python -m pip install -q -r (Join-Path $Root "requirements.txt")
}

$wsl = @()
if ($Distro) { $wsl += @("-d", $Distro) }

# Closing the emulator's window stops only the Docker client: the emulator
# carries on, and emu.sh would refuse to start another.
if (& wsl.exe @wsl --exec docker ps -q --filter 'name=^s60-emu$') {
    throw ("The emulator is still running from before (its window was closed without leaving it).`n" +
           "Rejoin it with:  wsl docker attach s60-emu`n" +
           "or stop it with: wsl docker stop s60-emu")
}
$wslRoot = (& wsl.exe @wsl -- wslpath -a ($Root -replace '\\', '/')).Trim()
$flags = @("--windows")
if ($Cloud) { $flags += "--cloud" }
if ($Fresh) { $flags += "--fresh" }
if ($Pcap) { $flags += "--pcap" }
if ($Rebuild) { $flags += "--rebuild" }
if ($NoFirewall) { $flags += "--no-firewall" }

# One command line, not a list: Start-Process joins a list without quoting.
# If emu.sh stops, its window stays open until its reason has been read.
$emu = "tools/emulator/emu.sh $($flags -join ' ') || read -rp 'emu.sh stopped - press Enter to close this window. '"
Start-Process wsl.exe -ArgumentList ((($wsl + @("--cd", "`"$wslRoot`"", "--exec", "bash", "-c")) -join " ") + " `"$emu`"")

# Only one link can hold the adapter.
$link = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'windows_link\.py' }
if ($link) {
    Write-Host "The link is already running in its own window - it carries on with this emulator."
} else {
    Start-Process $Python -WorkingDirectory $Root -ArgumentList "tools\emulator\windows_link.py"
}

Write-Host @"

The emulator is starting in its own window$(if ($Cloud) { " - confirm the real plug is unplugged there" }),
and the link in another. Once the link says Connected, run the conversion here:

    .venv\Scripts\python tools\s60_autoflash.py run

When it asks you to switch the plug off and on, type in the emulator window:
    plug restart | plug off | plug on | plug cycle 7
Your home Wi-Fi there is PICSimLabWifi, with no password.
"@
