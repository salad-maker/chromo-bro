[CmdletBinding()]
param([string]$Config = (Join-Path $PSScriptRoot 'local.json'))
$ErrorActionPreference = 'Stop'
$LocalConfig = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$ConfigPath=(Resolve-Path -LiteralPath $Config).Path
$BuilderScript=Join-Path $PSScriptRoot 'chromo_bro.py'
$ClientScript=Join-Path $LocalConfig.install_root 'runtime\chromo_client.py'
function Get-BuildParents {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe','pythonw.exe','python3.exe') -and $_.CommandLine -and
        $_.CommandLine -match '(?:^|\s)update(?:\s|$)' -and
        (($_.CommandLine.IndexOf($BuilderScript,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $_.CommandLine.IndexOf($ConfigPath,[StringComparison]::OrdinalIgnoreCase) -ge 0) -or
         $_.CommandLine.IndexOf($ClientScript,[StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
}
$Parents=Get-BuildParents
$OutputDirectory = [IO.Path]::GetFullPath((Join-Path $LocalConfig.build_root 'checkout\src\out\ChromoBro'))
$Workers = @(Get-CimInstance Win32_Process -Filter "name='siso.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($OutputDirectory, [StringComparison]::OrdinalIgnoreCase) -ge 0
})
$Task = Get-ScheduledTask -TaskName 'ChromoBro Stable Build' -ErrorAction SilentlyContinue
if ($Task -and $Task.State -eq 'Running') { $Task | Stop-ScheduledTask }
$Parents=@($Parents)+@(Get-BuildParents) | Sort-Object ProcessId -Unique
foreach($Parent in $Parents) {
    $Current=@(Get-BuildParents | Where-Object { $_.ProcessId -eq $Parent.ProcessId })
    if($Current.Count) { & taskkill.exe /PID $Parent.ProcessId /T /F }
}
# A stopped scheduled action can leave its compiler children running. Match this
# exact output directory before stopping that worker and its own descendants.
foreach ($Worker in $Workers) {
    $Current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $Worker.ProcessId)
    if ($Current -and $Current.Name -eq 'siso.exe' -and $Current.CommandLine -and
        $Current.CommandLine.IndexOf($OutputDirectory, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        & taskkill.exe /PID $Current.ProcessId /T /F
        if ($LASTEXITCODE -ne 0) { throw ('Could not stop compiler worker ' + $Current.ProcessId) }
    }
}
Write-Output 'Stopped this build instance. Cached outputs and the daily schedule are retained.'
