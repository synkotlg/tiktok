# setup.ps1 - installe les dependances et cree la tache planifiee.
#   .\setup.ps1                  installation + tache (reveil toutes les 20 min, et a l'ouverture de session)
#   .\setup.ps1 -Every 30        reveil toutes les 30 min
#   .\setup.ps1 -DailyOnly       pas de declenchement a l'ouverture de session
#   .\setup.ps1 -NoTask          dependances seulement
#   .\setup.ps1 -Remove          supprime la tache planifiee
# Les horaires ne sont plus ici : a chaque reveil, le script decide s'il fait une session (une par partie de
# la journee, debut au hasard, d'apres active_hours et day_sessions de config.json). Un reveil sans session
# se termine en moins d'une seconde, sans ouvrir le navigateur. Le PC n'est jamais sorti de veille.
param(
    [int]$Every = 20,
    [string]$Time = "",
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
if ($Time) {
    Write-Warning "-Time est obsolete : les sessions suivent active_hours et day_sessions de config.json."
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
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\tiktok_rotate.py`" run --auto" -WorkingDirectory $root
    # chaque jour a 00:00, repete toutes les $Every min pendant 24 h
    $wake = New-ScheduledTaskTrigger -Daily -At 00:00
    $wake.Repetition = (New-ScheduledTaskTrigger -Once -At 00:00 -RepetitionInterval (New-TimeSpan -Minutes $Every) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    $triggers = @($wake)
    if (-not $DailyOnly) {
        # PC allume en cours de journee : premier reveil 5 min apres l'ouverture de session
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
        $logon.Delay = "PT5M"
        $triggers += $logon
    }
    # IgnoreNew : pendant une session, les reveils suivants sont ignores par Windows
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Out-Null
    $when = if ($DailyOnly) { "reveil toutes les $Every min" } else { "reveil toutes les $Every min et 5 min apres chaque ouverture de session" }
    Write-Host "Tache $taskName creee : $when ; le script decide des sessions (config.json)."
}

Write-Host ""
Write-Host "Etapes suivantes (une seule fois) :"
Write-Host "  1. remplis seeds.txt avec des comptes de ta niche"
Write-Host "  2. connexion a TikTok :  .\.venv\Scripts\python.exe tiktok_rotate.py login"
Write-Host "  3. verification de l'interface :  .\.venv\Scripts\python.exe tiktok_rotate.py diagnose"
