# setup.ps1 - installe les dependances et cree la tache planifiee.
#   .\setup.ps1                  installation + tache (a chaque ouverture de session et chaque jour a 09:00)
#   .\setup.ps1 -Time 18:30      autre heure pour le declenchement quotidien
#   .\setup.ps1 -DailyOnly       pas de declenchement a l'ouverture de session
#   .\setup.ps1 -NoTask          dependances seulement
#   .\setup.ps1 -Remove          supprime la tache planifiee
param(
    [string]$Time = "09:00",
    [switch]$DailyOnly,
    [switch]$NoTask,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$taskName = "TikTokRotate"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Tache $taskName supprimee."
    return
}

Set-Location $root
if (-not (Test-Path ".venv")) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not $NoTask) {
    $pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\tiktok_rotate.py`" run" -WorkingDirectory $root
    $triggers = @(New-ScheduledTaskTrigger -Daily -At $Time -RandomDelay (New-TimeSpan -Minutes 45))
    if (-not $DailyOnly) {
        # PC allume plus tard dans la journee : le passage demarre 5 min apres l'ouverture de session
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
        $logon.Delay = "PT5M"
        $triggers += $logon
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
    $when = if ($DailyOnly) { "chaque jour a $Time (plus un delai aleatoire jusqu'a 45 min)" } else { "5 min apres chaque ouverture de session et chaque jour a $Time" }
    Write-Host "Tache $taskName creee : $when."
}

Write-Host ""
Write-Host "Etapes suivantes (une seule fois) :"
Write-Host "  1. remplis seeds.txt avec des comptes de ta niche"
Write-Host "  2. connexion a TikTok :  .\.venv\Scripts\python.exe tiktok_rotate.py login"
Write-Host "  3. verification de l'interface :  .\.venv\Scripts\python.exe tiktok_rotate.py diagnose"
