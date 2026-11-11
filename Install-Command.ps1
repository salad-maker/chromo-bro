[CmdletBinding()]
param(
    [string]$Config = (Join-Path $PSScriptRoot 'local.json'),
    [string]$Python = 'C:\Python39\python.exe'
)
$ErrorActionPreference = 'Stop'
$Config = (Resolve-Path -LiteralPath $Config).Path
$Python = (Resolve-Path -LiteralPath $Python).Path
$PythonWindowless = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $PythonWindowless)) { throw 'pythonw.exe is required' }
$Script = Join-Path $PSScriptRoot 'chromo_bro.py'
$LocalConfig = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$CommandDirectory = Join-Path ([IO.Path]::GetFullPath($LocalConfig.install_root)) 'commands'
New-Item -ItemType Directory -Path $CommandDirectory -Force | Out-Null
$CommandPath = Join-Path $CommandDirectory 'chromo.cmd'
if ((Test-Path -LiteralPath $CommandPath) -and (Get-Content -LiteralPath $CommandPath -Raw) -notmatch 'rem Managed by chromo-bro') {
    throw 'An unrelated chromo.cmd already exists; leaving it in place'
}
$Command = @(
    '@echo off'
    'rem Managed by chromo-bro'
    'setlocal DisableDelayedExpansion'
    ('start "" "{0}" "{1}" --config "{2}" launch %*' -f $PythonWindowless.Replace('%','%%'), $Script.Replace('%','%%'), $Config.Replace('%','%%'))
) -join "`r`n"
[IO.File]::WriteAllText($CommandPath, $Command + "`r`n", [Text.UTF8Encoding]::new($false))
$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$Entries = @($UserPath -split ';' | Where-Object { $_ })
if (-not @($Entries | Where-Object { $_.TrimEnd('\') -ieq $CommandDirectory.TrimEnd('\') }).Count) {
    [Environment]::SetEnvironmentVariable('Path', (($Entries + $CommandDirectory) -join ';'), 'User')
    # Let Explorer give subsequently opened terminals the updated environment.
    try {
        if (-not ('ChromoBro.EnvironmentNotice' -as [type])) {
            Add-Type @'
using System;
using System.Runtime.InteropServices;
namespace ChromoBro {
    public static class EnvironmentNotice {
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern IntPtr SendMessageTimeout(IntPtr window, uint message,
            UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
    }
}
'@
        }
        [UIntPtr]$MessageResult = [UIntPtr]::Zero
        [void][ChromoBro.EnvironmentNotice]::SendMessageTimeout([IntPtr]0xffff, 0x1a,
            [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$MessageResult)
    } catch {
        Write-Verbose 'PATH is saved; sign out and back in if a restarted terminal still has the old environment.'
    }
}
if (-not @($env:Path -split ';' | Where-Object { $_.TrimEnd('\') -ieq $CommandDirectory.TrimEnd('\') }).Count) {
    $env:Path += ';' + $CommandDirectory
}
Write-Output ('Installed command: ' + $CommandPath)
Write-Output 'Restart your terminal, then run: chromo'
