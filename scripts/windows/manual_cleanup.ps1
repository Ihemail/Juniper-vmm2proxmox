$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$StateDir = Join-Path (Split-Path -Parent (Split-Path -Parent $ScriptDir)) "state"

Write-Host "[MANUAL CLEANUP] Starting manual cleanup..."

$started = Join-Path $StateDir "started_vmids.json"
$bridges = Join-Path $StateDir "created_bridges.json"

if (Test-Path $started) {
    Write-Host "[MANUAL CLEANUP] Shutting down recorded VMs"
    & (Join-Path $ScriptDir "shutdown_all.ps1")
} else {
    Write-Host "[MANUAL CLEANUP] No started_vmids.json; skipping shutdown"
}

$reply = Read-Host "[MANUAL CLEANUP] Delete created vmc_* bridges? (yes/NO)"
if ($reply -match '^(y|yes)$') {
    if (Test-Path $bridges) {
        Write-Host "[MANUAL CLEANUP] Deleting created bridges"
        & (Join-Path $ScriptDir "delete_bridges.ps1")
    } else {
        Write-Host "[MANUAL CLEANUP] No created_bridges.json; skipping bridge delete"
    }
} else {
    Write-Host "[MANUAL CLEANUP] Skipping bridge deletion (default No)"
}

Write-Host "[MANUAL CLEANUP] Cleanup done."
