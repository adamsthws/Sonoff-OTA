# PSScriptAnalyzer (tools/repo/lint.py): what is likely to break, not style.
@{
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        # The scripts talk to a person at a console: Write-Host is what they mean.
        'PSAvoidUsingWriteHost',
        # The scripts' own helpers, not cmdlets anyone imports.
        'PSUseSingularNouns',
        'PSUseShouldProcessForStateChangingFunctions'
    )
    Rules        = @{
        # windows-start.ps1 and what it runs run on Windows PowerShell 5.1, which every
        # Windows has, as well as on PowerShell 7.
        PSUseCompatibleSyntax = @{
            Enable         = $true
            TargetVersions = @('5.1', '7.0')
        }
    }
}
