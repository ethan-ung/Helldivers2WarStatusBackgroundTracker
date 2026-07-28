<#
    Remove the scheduled task and put the original wallpaper back.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Helldivers2Wallpaper',
    [switch]$KeepWallpaper,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
}
else {
    Write-Host "No scheduled task named '$TaskName'."
}

if ($KeepWallpaper) {
    Write-Host 'Leaving the current wallpaper in place (-KeepWallpaper).'
    return
}

if ([string]::IsNullOrEmpty($PythonPath)) {
    $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $candidate) { $PythonPath = $candidate.Source }
    else { $PythonPath = "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" }
}

if (Test-Path $PythonPath) {
    Write-Host 'Restoring the original wallpaper...'
    & $PythonPath (Join-Path $repoRoot 'run_once.py') --restore
}
else {
    Write-Warning "Could not find python.exe; run 'python run_once.py --restore' yourself."
}
