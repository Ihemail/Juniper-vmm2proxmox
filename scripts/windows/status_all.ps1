param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ArgsList
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$PyScript = Join-Path $ScriptDir "status_all.py"
$Config = Join-Path $RepoRoot "config.yaml"
$StateDir = Join-Path $RepoRoot "state"

$pythonCmd = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$pythonArgs = if ($pythonCmd -eq "py") { @("-3") } else { @() }

& $pythonCmd @pythonArgs $PyScript --config $Config --state $StateDir @ArgsList
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
