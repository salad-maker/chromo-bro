[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference='Stop'
$Root=(Resolve-Path -LiteralPath $Root).Path
$Config=Get-Content -LiteralPath (Join-Path $Root 'runtime\client.json') -Raw | ConvertFrom-Json
$Python=Join-Path $Root 'runtime\python\pythonw.exe'
$Client=Join-Path $Root 'runtime\chromo_client.py'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Extract the complete portable package first' }
$Shell=New-Object -ComObject WScript.Shell
$Shortcut=$Shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Chromo.lnk'))
$Shortcut.TargetPath=Join-Path $Root 'Chromo.exe'
$Shortcut.WorkingDirectory=$Root
$Shortcut.IconLocation=(Join-Path $Root 'Chromo.exe') + ',0'
$Shortcut.Description='Chromo browser with your existing profile and verified updates.'
$Shortcut.Save()
$Menu=Join-Path ([Environment]::GetFolderPath('Programs')) 'Chromo'
New-Item -ItemType Directory -Path $Menu -Force | Out-Null
$StatusLink=$Shell.CreateShortcut((Join-Path $Menu 'Chromo Status.lnk'))
$StatusLink.TargetPath=Join-Path $Root 'Chromo.exe'
$StatusLink.Arguments='status'
$StatusLink.IconLocation=$Shortcut.IconLocation
$StatusLink.WorkingDirectory=$Root
$StatusLink.Save()
$CommandDirectory=Join-Path $Root 'commands'
$UserPath=[Environment]::GetEnvironmentVariable('Path','User')
$Entries=@($UserPath -split ';' | Where-Object { $_ })
if (-not @($Entries | Where-Object { $_.TrimEnd('\') -ieq $CommandDirectory.TrimEnd('\') }).Count) {
    [Environment]::SetEnvironmentVariable('Path',(($Entries+$CommandDirectory) -join ';'),'User')
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ChromoEnvironment {
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]
 public static extern IntPtr SendMessageTimeout(IntPtr h,uint m,UIntPtr w,string l,uint f,uint t,out UIntPtr r);
}
'@
    [UIntPtr]$Result=[UIntPtr]::Zero
    [void][ChromoEnvironment]::SendMessageTimeout([IntPtr]0xffff,0x1a,[UIntPtr]::Zero,'Environment',2,2000,[ref]$Result)
}
$Account=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal=New-ScheduledTaskPrincipal -UserId $Account -LogonType Interactive -RunLevel Limited
$Settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Days 3)
if ($Config.mode -eq 'source') {
    $Builder=(Resolve-Path -LiteralPath $Config.builder_config).Path
    $TaskName='ChromoBro Stable Build'
} else {
    $TaskName='ChromoBro Release Update'
}
$BuildAction=New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --root "{1}" update' -f $Client,$Root) -WorkingDirectory $Root
$Daily=New-ScheduledTaskTrigger -Daily -At '04:00'
$Logon=New-ScheduledTaskTrigger -AtLogOn -User $Account
$Logon.Delay='PT5M'
Register-ScheduledTask -TaskName $TaskName -Action $BuildAction -Trigger @($Daily,$Logon) -Settings $Settings -Principal $Principal -Force | Out-Null
$Apply=New-ScheduledTaskAction -Execute $Python -Argument ('"{0}" --root "{1}" apply' -f $Client,$Root) -WorkingDirectory $Root
$Trigger=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(15)) -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName 'ChromoBro Apply Pending Build' -Action $Apply -Trigger $Trigger -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Output 'Installed the Chromo desktop shortcut, status shortcut, terminal command, and update tasks.'
