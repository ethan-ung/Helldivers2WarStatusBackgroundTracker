<#
    Register (or repoint) the scheduled task that refreshes the wallpaper.

    A task of this name may already exist from an earlier implementation and
    point at a path that no longer exists, so this deletes and recreates rather
    than editing in place - that way the trigger is known-good.

    Runs pythonw.exe so no console window flashes every five minutes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Helldivers2Wallpaper',
    [int]$IntervalMinutes = 5,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$entryPoint = Join-Path $repoRoot 'run_once.py'

if (-not (Test-Path $entryPoint)) { throw "Entry point not found: $entryPoint" }

# ---------------------------------------------------------------- locate pythonw
if ([string]::IsNullOrEmpty($PythonPath)) {
    $candidate = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($null -ne $candidate) {
        $PythonPath = $candidate.Source
    }
    else {
        $guesses = @(
            "$env:LOCALAPPDATA\Programs\Python\Python314\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe"
        )
        foreach ($guess in $guesses) {
            if (Test-Path $guess) { $PythonPath = $guess; break }
        }
    }
}
if ([string]::IsNullOrEmpty($PythonPath) -or -not (Test-Path $PythonPath)) {
    throw 'Could not locate pythonw.exe. Pass -PythonPath explicitly.'
}

Write-Host "Python      : $PythonPath"
Write-Host "Entry point : $entryPoint"
Write-Host "Interval    : every $IntervalMinutes minutes"

# ---------------------------------------------------------------- remove the old task
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    $oldAction = $existing.Actions | Select-Object -First 1
    Write-Host "Replacing existing task (was: $($oldAction.Execute) $($oldAction.Arguments))"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ---------------------------------------------------------------- build the new task
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument "`"$entryPoint`" --once" -WorkingDirectory $repoRoot

$start = (Get-Date).Date.AddHours((Get-Date).Hour)
$trigger = New-ScheduledTaskTrigger -Once -At $start

# Setting Repetition via the CIM class is the reliable way to get an indefinite
# repeat; omitting Duration means "repeat forever".
$trigger.Repetition = New-CimInstance -ClassName MSFT_TaskRepetitionPattern `
    -Namespace 'Root/Microsoft/Windows/TaskScheduler' -ClientOnly `
    -Property @{ Interval = "PT${IntervalMinutes}M"; StopAtDurationEnd = $false }

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# Interactive logon: changing the desktop wallpaper requires the user's session.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger @($trigger, $logonTrigger) `
    -Settings $settings `
    -Principal $principal `
    -Description 'Renders the Helldivers 2 galactic war status as the desktop wallpaper.' | Out-Null

Write-Host ''
Write-Host "Registered '$TaskName'." -ForegroundColor Green
Write-Host 'Starting an initial run...'
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 2
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List TaskName, LastRunTime, LastTaskResult, NextRunTime
Write-Host 'LastTaskResult 0 means success; 267009 means it is still running.'
