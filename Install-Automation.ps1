[CmdletBinding()]
param(
    [string]$Config = (Join-Path $PSScriptRoot 'local.json'),
    [string]$Python = 'C:\Python39\python.exe'
)
$ErrorActionPreference = 'Stop'
$Config = (Resolve-Path -LiteralPath $Config).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$PythonWindowless = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonWindowless)) { throw 'pythonw.exe is required for the desktop launcher' }
$Script = Join-Path $PSScriptRoot 'chromo_bro.py'
$Account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 3)
$BuildAction = New-ScheduledTaskAction -Execute $PythonWindowless `
    -Argument ('"{0}" update --config "{1}"' -f $Script, $Config) -WorkingDirectory $PSScriptRoot
$BuildTrigger = New-ScheduledTaskTrigger -Daily -At '04:00'
$LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $Account
$LogonTrigger.Delay = 'PT5M'
Register-ScheduledTask -TaskName 'ChromoBro Stable Build' -Action $BuildAction `
    -Trigger @($BuildTrigger, $LogonTrigger) -Settings $Settings -Principal $Principal `
    -Description 'Build and verify the latest Chromium Windows Stable source when a new release is available.' -Force | Out-Null
$ApplyAction = New-ScheduledTaskAction -Execute $PythonWindowless `
    -Argument ('"{0}" install-pending --config "{1}"' -f $Script, $Config) -WorkingDirectory $PSScriptRoot
$ApplyTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(15)) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName 'ChromoBro Apply Pending Build' -Action $ApplyAction `
    -Trigger $ApplyTrigger -Settings $Settings -Principal $Principal `
    -Description 'Apply a verified locally built Chromium update only while this Chromium installation is closed.' -Force | Out-Null
$Shell = New-Object -ComObject WScript.Shell
$LinkPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Chromium (chromo-bro).lnk'
$Shortcut = $Shell.CreateShortcut($LinkPath)
$Shortcut.TargetPath = $PythonWindowless
$Shortcut.Arguments = '"{0}" launch --config "{1}"' -f $Script, $Config
$Shortcut.WorkingDirectory = $PSScriptRoot
$LocalConfig = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$Shortcut.IconLocation = (Join-Path $LocalConfig.install_root 'bin\chrome.exe') + ',0'
$Shortcut.Description = 'Chromium Stable built locally from upstream source; reuses the existing profile.'
$Shortcut.Save()
& (Join-Path $PSScriptRoot 'Install-Command.ps1') -Config $Config -Python $Python
Get-ScheduledTask -TaskName 'ChromoBro Stable Build','ChromoBro Apply Pending Build' |
    Select-Object TaskName, State, Principal
Write-Output ('Desktop launcher: ' + $LinkPath)
