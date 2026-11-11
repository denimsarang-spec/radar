# Registers the local radar tasks for the current user (no admin needed). Safe to re-run.
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less UTF-8 as the ANSI codepage
# and multibyte comments can swallow the following line.
#   powershell -ExecutionPolicy Bypass -File D:\radar\scripts\setup_local.ps1
# Remove: Unregister-ScheduledTask RadarLive,RadarLLM,RadarFinal -Confirm:$false
$ErrorActionPreference = "Stop"
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
$user = $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

# Live server: starts at logon, restarts if it dies
$liveAction = New-ScheduledTaskAction -Execute $pyw -Argument "-W ignore D:\radar\live.py --no-open" -WorkingDirectory "D:\radar"
$liveTrigger = New-ScheduledTaskTrigger -AtLogOn -User $user
Register-ScheduledTask -TaskName RadarLive -Force -Settings $settings -Action $liveAction -Trigger $liveTrigger | Out-Null

# Morning LLM research: weekdays 08:00, runs right after wake-up if missed
$llmAction = New-ScheduledTaskAction -Execute $pyw -Argument "-W ignore D:\radar\scripts\llm_daily.py" -WorkingDirectory "D:\radar"
$llmTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:00
Register-ScheduledTask -TaskName RadarLLM -Force -Settings $settings -Action $llmAction -Trigger $llmTrigger | Out-Null

# Final run: weekdays 16:35, after closing prices are final (FINAL_AT 16:30); saves data/<date>.json
$finalAction = New-ScheduledTaskAction -Execute $pyw -Argument "-W ignore D:\radar\radar.py" -WorkingDirectory "D:\radar"
$finalTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:35
Register-ScheduledTask -TaskName RadarFinal -Force -Settings $settings -Action $finalAction -Trigger $finalTrigger | Out-Null

Get-ScheduledTask RadarLive,RadarLLM,RadarFinal | Select-Object TaskName,State
Write-Host "OK: RadarLive, RadarLLM, RadarFinal registered"
