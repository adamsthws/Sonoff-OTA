<#
Fetch or update Autoflash, the conversion tool, and start it, on Windows -
no git. The README's one line, in a terminal (or the Run box):

    powershell -c "irm https://github.com/adamsthws/Sonoff-OTA/releases/latest/download/liberate.ps1 | iex"

It, the release's files and its tag are assets of every release
(.github/workflows/release.yml), so only a released, tested version is ever
run - and no GitHub API, with its hourly limit, is asked.

The latest release is unpacked into %USERPROFILE%\Sonoff-OTA and started there,
as start.cmd starts it. Run again, the folder is updated in place: the files
the release it holds put there are replaced or removed, nothing else -
captures\, with the plugs' Sonoff backups and an unfinished conversion, and
the portable Python in .python\ stay as they are. A git clone there is left to
git. It is all one function, so pasted into an open PowerShell it leaves that
window's settings as they were, and returns rather than closing it.
#>
function Start-Autoflash {
    $ErrorActionPreference = "Stop"
    # Windows PowerShell 5.1's progress bar slows a download many times over.
    $ProgressPreference = "SilentlyContinue"
    # Windows PowerShell 5.1 on an older Windows 10 offers GitHub no TLS it takes.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = "adamsthws/Sonoff-OTA"
    $github = if ($env:AUTOFLASH_GITHUB) { $env:AUTOFLASH_GITHUB } else { "https://github.com" }
    $folder = if ($env:AUTOFLASH_HOME) { $env:AUTOFLASH_HOME } else { Join-Path $env:USERPROFILE "Sonoff-OTA" }
    # The installed release's tag, then every file it put in the folder.
    $manifest = Join-Path $folder ".installed-release"

    if (Test-Path (Join-Path $folder ".git")) {
        Write-Host " $folder is a git clone: starting it as it is (git pull updates it)."
    } else {
        if ((Test-Path $folder) -and -not (Test-Path $manifest) -and
            -not (Test-Path (Join-Path (Join-Path $folder "tools") "autoflash")) -and
            (Get-ChildItem -Force $folder | Select-Object -First 1)) {
            Write-Host " $folder already holds something else: nothing was changed. Move it away, or"
            Write-Host " set AUTOFLASH_HOME to another folder, and run this again."
            return
        }
        $installed = if (Test-Path $manifest) { Get-Content $manifest -TotalCount 1 } else { $null }
        $work = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
        New-Item -ItemType Directory $work | Out-Null
        try {
            # To a file: an asset comes as bytes, which .Content would not make text.
            $version = Join-Path $work "autoflash-version"
            try {
                Invoke-WebRequest "$github/$repo/releases/latest/download/autoflash-version" -OutFile $version -UseBasicParsing
                $latest = (Get-Content $version -TotalCount 1).Trim()
            } catch { $latest = $null }
            if ($latest -notmatch '^v[0-9]') { $latest = $null }
            if (-not $latest) {
                if (-not $installed) {
                    Write-Host " The latest release could not be found on GitHub: is this computer online?"
                    return
                }
                Write-Host " Could not check for a newer release: starting Autoflash $installed."
            } elseif ($latest -ne $installed) {
                Write-Host " Downloading Autoflash $latest..."
                $zip = Join-Path $work "autoflash.zip"
                try { Invoke-WebRequest "$github/$repo/releases/download/$latest/autoflash.zip" -OutFile $zip -UseBasicParsing }
                catch {
                    Write-Host " The download failed: $($_.Exception.Message)"
                    return
                }
                Expand-Archive $zip -DestinationPath (Join-Path $work "release")
                $release = (Get-ChildItem -Directory (Join-Path $work "release") | Select-Object -First 1).FullName
                $files = @(Get-ChildItem -Recurse -File -Force $release | ForEach-Object {
                    $_.FullName.Substring($release.Length + 1).Replace("\", "/")
                } | Sort-Object)
                foreach ($file in $files) {
                    $target = Join-Path $folder $file
                    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
                    Copy-Item -Force (Join-Path $release $file) $target
                }
                # What the release before had and this one has not: never a file
                # of the user's own, which no release lists.
                if (Test-Path $manifest) {
                    $keep = @{}
                    foreach ($file in $files) { $keep[$file] = $true }
                    foreach ($gone in (Get-Content $manifest | Select-Object -Skip 1)) {
                        $target = Join-Path $folder $gone
                        if ($keep.ContainsKey($gone) -or -not (Test-Path -PathType Leaf $target)) { continue }
                        Remove-Item -Force $target
                        # Its folder too, and theirs, once nothing is left in them.
                        $parent = Split-Path $target
                        while ($parent.Length -gt $folder.Length -and -not (Get-ChildItem -Force $parent | Select-Object -First 1)) {
                            Remove-Item -Force $parent
                            $parent = Split-Path $parent
                        }
                    }
                }
                # Last, so an install cut short is done again on the next run.
                Set-Content -Encoding UTF8 "$manifest.new" (@($latest) + $files)
                Move-Item -Force "$manifest.new" $manifest
                Write-Host " Autoflash $latest is in $folder."
            }
        } finally {
            Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
        }
    }

    # What start.cmd does, run on this same PowerShell.
    $powershell = (Get-Process -Id $PID).Path
    & $powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path (Join-Path $folder "tools") "windows-start.ps1") @args
}

Start-Autoflash @args
