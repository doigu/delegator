param(
    [string]$BinDir = (Join-Path $env:USERPROFILE '.local/bin')
)
$ErrorActionPreference = 'Stop'
$entrypoint = Join-Path $PSScriptRoot 'delegator.py'
$null = New-Item -ItemType Directory -Force -Path $BinDir
$quoted = $entrypoint.Replace("'", "''")
$launcher = "& python -X utf8 -B '$quoted' @args`nexit `$LASTEXITCODE`n"
Set-Content -LiteralPath (Join-Path $BinDir 'delegator.ps1') -Value $launcher -Encoding utf8
$quotaOut = Join-Path $env:USERPROFILE '.agents/delegator-quota.html'
$quotaCmd = @(
    '@echo off'
    'echo Refreshing delegator quota dashboard...'
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $BinDir 'delegator.ps1')`" quota --format html --out `"$quotaOut`""
    'if %ERRORLEVEL%==0 ('
    "  start `"`" `"$quotaOut`""
    ') else ('
    '  echo delegator quota failed with exit code %ERRORLEVEL%.'
    '  pause'
    ')'
) -join "`r`n"
Set-Content -LiteralPath (Join-Path $BinDir 'delegator-quota.cmd') -Value $quotaCmd -Encoding ascii
Write-Output "Launcher: $(Join-Path $BinDir 'delegator.ps1')"
Write-Output "Quota refresh shortcut: $(Join-Path $BinDir 'delegator-quota.cmd') (copy anywhere and double-click)"
Write-Output 'Use a directory already in PATH, or add it yourself. Run this installer again if the installed skill moves.'
