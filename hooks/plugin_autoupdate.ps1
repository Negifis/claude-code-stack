# Claude Code plugin auto-updater.
# Launched from the SessionStart hook. The launcher phase throttles and then
# re-spawns itself as a hidden, DETACHED worker so it never blocks session start.
# Plugin updates require a restart to apply, so changes take effect next launch.
#
# Throttle: at most once per $ThrottleHours. Delete the stamp file to force a run.
# Log: %USERPROFILE%\.claude\hooks\plugin_autoupdate.log

param([switch]$Worker)

$ThrottleHours = 12
$hookDir = Join-Path $env:USERPROFILE '.claude\hooks'
$stamp   = Join-Path $hookDir '.plugin_autoupdate.stamp'
$log     = Join-Path $hookDir 'plugin_autoupdate.log'

# ---- Launcher phase: throttle, then detach a hidden worker and return fast ----
if (-not $Worker) {
    if (Test-Path $stamp) {
        if (((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalHours -lt $ThrottleHours) { exit 0 }
    }
    # Record the attempt up front (prevents concurrent runs and hammering).
    Set-Content -Path $stamp -Value (Get-Date -Format o) -Force
    Start-Process -WindowStyle Hidden -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Worker'
    ) | Out-Null
    exit 0
}

# ---- Worker phase: refresh marketplaces and update every installed plugin ----
$ErrorActionPreference = 'SilentlyContinue'

function Write-Log([string]$m) {
    "{0}  {1}" -f (Get-Date -Format s), $m | Out-File -FilePath $log -Append -Encoding utf8
}

# Keep the log from growing unbounded.
if ((Test-Path $log) -and (Get-Item $log).Length -gt 204800) { Remove-Item $log -Force }
Write-Log '=== plugin auto-update start ==='

# Resolve the claude CLI (PATH first, then known install location).
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) {
    $fallback = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $fallback) { $claude = $fallback }
}
if (-not $claude) { Write-Log 'claude CLI not found; aborting.'; exit 0 }

try {
    Write-Log 'Refreshing marketplaces...'
    (& $claude plugin marketplace update 2>&1) | ForEach-Object { Write-Log "  $_" }

    $plugins = (& $claude plugin list --json 2>$null | Out-String | ConvertFrom-Json)
    foreach ($p in $plugins) {
        if (-not $p.id) { continue }
        $scope = if ($p.scope) { $p.scope } else { 'user' }
        Write-Log ("Updating {0} (scope={1}, current={2})..." -f $p.id, $scope, $p.version)
        (& $claude plugin update $p.id --scope $scope 2>&1) | ForEach-Object { Write-Log "  $_" }
    }
    Write-Log '=== plugin auto-update done ==='
} catch {
    Write-Log ("ERROR: {0}" -f $_.Exception.Message)
}
exit 0
