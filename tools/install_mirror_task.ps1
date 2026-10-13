

param(
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$State = "$env:LOCALAPPDATA\waam-mirror\state.json",
    [string]$Staging = "$env:LOCALAPPDATA\waam-mirror\staging",
    [string]$Name = 'WAAM OneDrive copy'
)
$ErrorActionPreference = 'Stop'
$tool = Join-Path $Repo 'tools\mirror_runs.py'
foreach ($path in @($Python, $tool, $Dest)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Not found: $path" }
}
$arguments = '"{0}" --dest "{1}" --state "{2}" --staging "{3}"' -f $tool, $Dest, $State, $Staging
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'Copies every file the WAAM workbench keeps to the selected destination folder (tools/mirror_runs.py).' |
    Out-Null
$task = Get-ScheduledTask -TaskName $Name
"{0}: {1}, every {2}" -f $task.TaskName, $task.State, $task.Triggers[0].Repetition.Interval
