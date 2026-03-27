[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgsList
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python "$ScriptDir/update_installed_skill.py" @ArgsList
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 "$ScriptDir/update_installed_skill.py" @ArgsList
    exit $LASTEXITCODE
}

throw "Python 3 not found. Please install Python first."
