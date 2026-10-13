<#
The Python the conversion tool runs on, on Windows: dot-sourced by start.ps1,
tools\emulator\emu.ps1, windows-bootstrap.ps1 and the emulator's Windows VM.

The tool needs Python 3.10 or newer, 64-bit, and nothing beyond its standard
library. Get-ToolPython, in order:

1. python.org's portable ("embeddable") Python, pinned: the same Python for
   everyone, and nothing installed - it is unpacked into .python\ in the
   checkout, and deleting the checkout removes it.
2. If that cannot be had or run (no internet, a policy that blocks programs in
   user folders), a Python already installed - from python.org, the Microsoft
   Store, winget, Chocolatey, Scoop, conda or uv.
3. If there is none, python.org's installer, quietly, for this account only -
   no administrator rights. It is recorded, and Request-PythonRemoval offers
   to remove it again when the tool exits.
#>

# No progress bars while it sets up: not the download's, not PowerShell's own
# "Preparing modules for first use" - nothing a person needs to watch. (Each
# file that dot-sources this one is quiet with it, to its end.)
$ProgressPreference = "SilentlyContinue"

$PortablePython = @{
    Name = "portable Python 3.12.10 (python.org)"
    Folder = "windows-3.12.10"
    Url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
    Size = "about 11 MB"
    # python.org publishes MD5 (fe8ef205f2e9c3ba44d0cf9954e1abd3), which this file matches.
    Sha256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
}
# Where the portable Python, and the record of an installed one, are kept.
$PythonHome = if ($env:S60_PYTHON_HOME) { $env:S60_PYTHON_HOME } else { Join-Path (Split-Path $PSScriptRoot) ".python" }
$InstalledMarker = Join-Path $PythonHome "installed-by-s60.json"

$PythonInstaller = @{
    Name = "Python 3.12 (64-bit, from python.org)"
    Url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    Size = "about 27 MB"
    # python.org publishes MD5 (5eddb0b6f12c852725de071ae681dde4), which this
    # file matches, and winget's manifest has this same SHA-256.
    Sha256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
}

function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Get-Pinned($what, $pin, $dir = (Join-Path $env:TEMP "s60-downloads")) {
    # Downloaded once, and used only if it is exactly the pinned file.
    New-Item -ItemType Directory -Force $dir | Out-Null
    $file = Join-Path $dir ([IO.Path]::GetFileName($pin.Url))
    if (-not ((Test-Path $file) -and (Get-FileHash $file).Hash -eq $pin.Sha256)) {
        # The size, as there is nothing else to watch while it comes. Not
        # Write-Output: that would join the returned path.
        Write-Host " Downloading $what$(if ($pin.Size) { ", $($pin.Size)" })..."
        Invoke-WebRequest -Uri $pin.Url -OutFile $file -UseBasicParsing   # quiet, as above: many times slower with a bar
    }
    $hash = (Get-FileHash $file).Hash
    if ($hash -ne $pin.Sha256) {
        Remove-Item $file -Force
        throw "$what download does not match its pinned SHA-256 (got $hash) - not run"
    }
    return $file
}

function Get-PythonCandidates {
    # Everywhere a Python is commonly found, most likely first: the py
    # launcher; python and python3 on PATH (python.org, the Microsoft Store,
    # Chocolatey, Scoop's shims, an activated conda); the registry, where
    # installers record themselves (PEP 514) - which finds them off PATH too;
    # and the usual homes of conda, Scoop and uv, which often stay off PATH.
    $found = [System.Collections.Generic.List[object]]::new()
    $add = { param($how, $exe, $arguments) $found.Add([pscustomobject]@{ How = $how; Exe = $exe; Arguments = $arguments }) }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { & $add "py launcher" $py.Source @("-3") }
    foreach ($name in "python", "python3") {
        Get-Command $name -All -ErrorAction SilentlyContinue | ForEach-Object { & $add "PATH" $_.Source @() }
    }
    foreach ($root in "HKCU:\Software\Python", "HKLM:\Software\Python", "HKLM:\Software\WOW6432Node\Python") {
        Get-ChildItem $root -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -eq "InstallPath" } |
            ForEach-Object {
                $key = Get-ItemProperty $_.PSPath
                $exe = if ($key.ExecutablePath) { $key.ExecutablePath } elseif ($key.'(default)') { Join-Path $key.'(default)' "python.exe" }
                if ($exe) { & $add "registry" $exe @() }
            }
    }
    $homes = @("$env:USERPROFILE\miniconda3", "$env:USERPROFILE\anaconda3", "$env:LOCALAPPDATA\miniconda3",
               "$env:LOCALAPPDATA\anaconda3", "$env:ProgramData\miniconda3", "$env:ProgramData\anaconda3",
               "$env:USERPROFILE\scoop\apps\python\current")
    foreach ($dir in $homes) { & $add "usual folder" (Join-Path $dir "python.exe") @() }
    Get-ChildItem "$env:APPDATA\uv\python" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { & $add "uv" (Join-Path $_.FullName "python.exe") @() }
    return $found
}

function Test-PythonCandidate($candidate) {
    # Run it: that tells a real Python from the Store's stub, which only says
    # "Python was not found" and exits 9009. 3.10 and 64-bit are the tool's
    # needs. No quotes in the probe: Windows PowerShell mangles them in a
    # native argument.
    $probe = 'import sys; assert sys.version_info >= (3, 10) and sys.maxsize > 2**32; print(sys.executable)'
    if (-not (Test-Path $candidate.Exe)) { return $null }
    try {
        $out = & $candidate.Exe @($candidate.Arguments) -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ([string]($out | Select-Object -Last 1)).Trim() }
    } catch {}
    return $null
}

function Find-Python {
    foreach ($candidate in Get-PythonCandidates) {
        $python = Test-PythonCandidate $candidate
        if ($python) { return $python }
    }
    return $null
}

function Show-PythonCandidates {
    $first = $true
    foreach ($candidate in Get-PythonCandidates) {
        if (-not (Test-Path $candidate.Exe)) { continue }
        $python = Test-PythonCandidate $candidate
        $verdict = if (-not $python) { "not usable (the Store's stub, older than 3.10, or 32-bit)" }
                   elseif ($first) { $first = $false; "USED: $python" } else { "usable: $python" }
        Write-Host (" {0,-12} {1}`n              {2}" -f $candidate.How, $candidate.Exe, $verdict)
    }
    if ($first) { Write-Host " No usable Python: $($PythonInstaller.Name) would be installed." }
}

function Install-Python {
    # For this account only: no administrator rights, and nothing changed for
    # anyone else on the computer. It adds itself, and the py launcher, to PATH.
    # The installer is downloaded into .python\ and kept there, beside the
    # record of it: it is what removes it. Nothing of it outside that folder.
    $installer = Get-Pinned $PythonInstaller.Name $PythonInstaller $PythonHome
    Write-Host " Installing $($PythonInstaller.Name), for your account..."
    $p = Start-Process $installer -Wait -PassThru -ArgumentList `
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "InstallLauncherAllUsers=0", "Include_test=0"
    if ($p.ExitCode -ne 0) { throw "the Python installer failed (exit $($p.ExitCode))" }
    @{ name = $PythonInstaller.Name; installer = $installer } | ConvertTo-Json | Set-Content $InstalledMarker
    Update-SessionPath
}

function Get-PortablePython {
    # python.exe in .python\, unpacked from python.org's pinned zip if need
    # be; $null when it cannot be had or will not run.
    $dir = Join-Path $PythonHome $PortablePython.Folder
    $exe = Join-Path $dir "python.exe"
    try {
        if (-not (Test-Path $exe)) {
            # Downloaded into .python\ too, and the zip gone once unpacked:
            # nothing of it outside this folder.
            $zip = Get-Pinned $PortablePython.Name $PortablePython $PythonHome
            $partial = "$dir.partial"
            Remove-Item $partial -Recurse -Force -ErrorAction SilentlyContinue
            # Not Expand-Archive: it shows a progress bar of its own, and is
            # slow with it.
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            [IO.Compression.ZipFile]::ExtractToDirectory($zip, $partial)
            Move-Item $partial $dir
            Remove-Item $zip -Force
        }
        return Test-PythonCandidate ([pscustomobject]@{ How = "portable"; Exe = $exe; Arguments = @() })
    } catch {
        Write-Host " The portable Python could not be used ($($_.Exception.Message)); looking for an installed one."
        return $null
    }
}

function Get-ToolPython {
    # Portable; else installed; else installed now, quietly. Throws if none.
    $python = Get-PortablePython
    if ($python) { return $python }
    $python = Find-Python
    if ($python) { return $python }
    Install-Python
    $python = Find-Python
    if (-not $python) { throw "Python was installed but cannot be found yet - open a new window and try again" }
    return $python
}

function Request-PythonRemoval {
    # After the tool: if this tool installed Python, offer to remove it, so the
    # computer can be left as it was. Keeping it is the default.
    if (-not (Test-Path $InstalledMarker)) { return }
    $record = Get-Content $InstalledMarker -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host " $($record.name) was installed for this tool. Remove it now, to leave nothing"
    Write-Host " behind? If you will run the tool again - for another plug, or to finish this"
    Write-Host " one - keep it: the next run then starts straight away."
    $answer = "$(Read-Host ' Remove Python now? [y/N]')"
    if ($answer.Trim() -notmatch '^(y|yes)$') { Write-Host " Kept."; return }
    $p = Start-Process $record.installer -Wait -PassThru -ArgumentList "/uninstall", "/quiet"
    if ($p.ExitCode -ne 0) { Write-Host " Removing it failed (exit $($p.ExitCode)): use Settings > Apps instead."; return }
    Remove-Item $InstalledMarker, $record.installer -Force -ErrorAction SilentlyContinue
    Update-SessionPath
    Write-Host " Removed."
}
