# GEO-analysis : register / unregister the auto-update task (ASCII only on purpose:
# Windows PowerShell 5.1 reads BOM-less scripts in the ANSI codepage).
#
# The task runs generate.py every hour from 14:30 to 23:30, every day, with no window
# (pythonw). generate.py --skip-if-unchanged regenerates analysis.html and copies it to
# share_dirs (Box) only when monitoring produced new results / reports.
#
#   register_auto_update.bat              register (or update) the task
#   register_auto_update.bat -Unregister  remove the task
#   register_auto_update.bat -Time 15:00  change the first run time
param(
  [string]$Time = "14:30",
  [switch]$Unregister
)
$ErrorActionPreference = "Stop"
$TaskName = "GEO_Analysis_AutoUpdate"
$AppDir = Split-Path -Parent $PSScriptRoot

if ($Unregister) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] Removed task: $TaskName"
  } else {
    Write-Host "[INFO] Task not found: $TaskName"
  }
  exit 0
}

# Python: same search order as the one-click bat (Anaconda first). pythonw = no console window.
$candidates = @(
  "C:\work\anaconda_install\pythonw.exe",
  "$env:USERPROFILE\anaconda3\pythonw.exe",
  "$env:USERPROFILE\Anaconda3\pythonw.exe",
  "$env:LOCALAPPDATA\anaconda3\pythonw.exe"
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { throw "pythonw.exe (Anaconda) not found. Checked: $($candidates -join ', ')" }

$log = Join-Path $AppDir "logs\auto_update.log"
$argsLine = '"{0}" --skip-if-unchanged --log "{1}"' -f (Join-Path $AppDir "generate.py"), $log
$action = New-ScheduledTaskAction -Execute $py -Argument $argsLine -WorkingDirectory $AppDir

$start = [datetime]::ParseExact($Time, "HH:mm", $null)
$trigger = New-ScheduledTaskTrigger -Daily -At $start
# repeat every hour for 9 hours (14:30 .. 23:30 with the default time)
$rep = (New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours 9)).Repetition
$trigger.Repetition = $rep

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# Run as the logged-on user (Box Drive is only available in the user session).
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "GEO-analysis: regenerate analysis.html and copy to Box when monitoring has new data" -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$i = $t | Get-ScheduledTaskInfo
Write-Host "[OK] Registered task: $TaskName"
Write-Host "     Python : $py"
Write-Host "     Runs   : every day $Time, then hourly for 9 hours (skips when nothing changed)"
Write-Host "     Next   : $($i.NextRunTime)"
Write-Host "     Log    : $log"
