# PSScriptAnalyzer on the files given, for tools/repo/lint.py, with the settings
# in PSScriptAnalyzerSettings.psd1: each finding, and exit 1 if there is one.
param([Parameter(ValueFromRemainingArguments)] [string[]] $Files)
$ErrorActionPreference = 'Stop'
$settings = Join-Path $PSScriptRoot 'PSScriptAnalyzerSettings.psd1'
$found = @($Files | ForEach-Object { Invoke-ScriptAnalyzer -Path $_ -Settings $settings })
foreach ($finding in $found) {
    $where = Resolve-Path -Relative $finding.ScriptPath
    Write-Output ("{0}:{1}:{2}: {3} {4}: {5}" -f $where, $finding.Line, $finding.Column,
        $finding.Severity, $finding.RuleName, $finding.Message)
}
if ($found.Count) { exit 1 }
