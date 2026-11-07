# 로컬 레이더 작업 등록 (본인 계정, 관리자 권한 불필요). 다시 실행해도 덮어쓰기만 한다.
#   powershell -ExecutionPolicy Bypass -File D:\radar\scripts\setup_local.ps1
# 해제: Unregister-ScheduledTask RadarLive,RadarLLM -Confirm:$false
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
$user = $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

# 실시간 서버: 로그온할 때 시작, 죽으면 재시작
Register-ScheduledTask -TaskName RadarLive -Force -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute $pyw -Argument "-W ignore D:\radar\live.py --no-open" -WorkingDirectory "D:\radar") `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) | Out-Null

# 아침 LLM 조사: 평일 08:00 (놓치면 켜진 직후 실행)
Register-ScheduledTask -TaskName RadarLLM -Force -Settings $settings `
    -Action (New-ScheduledTaskAction -Execute $pyw -Argument "-W ignore D:\radar\scripts\llm_daily.py" -WorkingDirectory "D:\radar") `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:00) | Out-Null

Get-ScheduledTask RadarLive,RadarLLM | Select-Object TaskName,State
