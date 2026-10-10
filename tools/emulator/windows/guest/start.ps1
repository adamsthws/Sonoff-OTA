<#
At each logon (the hook firstlogon.ps1 adds). vm-run.sh layer leaves the name
of a layer to set up in C:\S60\layer.txt: its script runs, and shuts Windows
down. In a test run: wire Windows into the emulator's home network, and open
a window at the checkout to run the conversion tool in. vm-run.sh
puts the checkout in C:\S60\repo, and the emulator's address and the console to
run in in C:\S60\run.json: OpenConsole - Windows Terminal's console, as on
Windows 11 (Terminal itself needs XAML, which Server Core lacks) - or the
classic one, as on Windows 10.
#>
if (Test-Path C:\S60\layer.txt) {
    $layer = (Get-Content C:\S60\layer.txt -Raw).Trim()
    Remove-Item C:\S60\layer.txt
    & "C:\S60\$layer.ps1"
    exit 0
}
$run = "C:\S60\run.json"
if (-not (Test-Path $run)) { exit 0 }  # a layer itself, booted by hand: nothing to start
$config = Get-Content $run -Raw | ConvertFrom-Json
$repo = "C:\S60\repo"
. "$repo\tools\windows-python.ps1"
# The link's own Python (rig.ps1), never the tool's: in a --first-run there is
# none yet, and the tool must find that so.
$python = Join-Path (Join-Path C:\S60\rig-python $PortablePython.Folder) python.exe

Start-Process $python -ArgumentList "tools\emulator\windows_link.py", "--host", $config.link_host `
    -WorkingDirectory $repo
$welcome = @"
`$host.UI.RawUI.WindowTitle = 'S60 conversion - $(if ($config.console -eq "openconsole") { "OpenConsole" } else { "classic console" })'
Write-Host ''
Write-Host 'Windows is on the emulated home Wi-Fi once the link window says Connected.'
Write-Host 'Run the conversion here, as a Windows user would:'
Write-Host ''
Write-Host '    .\start.ps1'
Write-Host ''
Write-Host 'Switch the plug off and on in the emulator, on the Linux side: plug restart'
Write-Host 'Your home Wi-Fi there is PICSimLabWifi, with no password.'
Write-Host ''
"@
$openconsole = "C:\S60\openconsole\OpenConsole.exe"
if ($config.console -eq "openconsole" -and (Test-Path $openconsole)) {
    # Its command line goes to PowerShell as one string: encoded, nothing to quote.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($welcome))
    Start-Process $openconsole -WorkingDirectory $repo `
        -ArgumentList "powershell.exe -NoExit -EncodedCommand $encoded"
} else {
    Start-Process powershell -WorkingDirectory $repo -ArgumentList "-NoExit", "-Command", $welcome
}
