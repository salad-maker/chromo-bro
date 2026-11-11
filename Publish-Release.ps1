[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Directory,
    [string]$Repository='salad-maker/chromo-bro',
    [string]$GitHubCli='gh'
)
$ErrorActionPreference='Stop'
$Directory=(Resolve-Path -LiteralPath $Directory).Path
$Manifest=Get-Content -LiteralPath (Join-Path $Directory 'release-manifest.json') -Raw | ConvertFrom-Json
$Tag='v{0}-chromium-{1}' -f $Manifest.application_version,$Manifest.version
$Files=@($Manifest.portable_asset.name,$Manifest.browser_asset.name,'release-manifest.json','SHA256SUMS.txt')
foreach($Name in $Files) {
    if([IO.Path]::GetFileName($Name) -ne $Name) { throw 'Release asset must be a plain filename' }
    if(-not(Test-Path -LiteralPath (Join-Path $Directory $Name))) { throw ('Missing release asset: '+$Name) }
}
foreach($Asset in @($Manifest.portable_asset,$Manifest.browser_asset)) {
    $stream=[IO.File]::OpenRead((Join-Path $Directory $Asset.name))
    $sha=[Security.Cryptography.SHA256]::Create()
    try { $actual=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose();$stream.Dispose() }
    if($actual -ne $Asset.sha256) { throw ('Release asset SHA-256 mismatch: '+$Asset.name) }
}
& $GitHubCli auth status --hostname github.com
if($LASTEXITCODE -ne 0) { throw 'Authenticate GitHub CLI first: gh auth login --web' }
$Notes=Join-Path $Directory 'release-notes.md'
@"
Chromo $($Manifest.application_version), built from Chromium Stable $($Manifest.version).

- Portable Windows x64 browser with a private Python runtime. Extract the complete ZIP and run Setup.exe.
- Chromo desktop shortcut and short terminal command.
- chromo status, status --watch, update, cleanup, and verify.
- Two matching browser/profile rollback sets, scoped staging cleanup, and interrupted-install recovery.
- Desktop failure notifications with persistent status and logs.
- SHA-256 hashes for portable and browser-only packages. Source commit: $($Manifest.source_commit).

Independent, unsigned Chromium build. Google Sync credentials and Widevine DRM are not bundled. Existing source builders retain local builds; portable users download verified published releases.
"@ | Set-Content -LiteralPath $Notes -Encoding UTF8
$Arguments=@('release','create',$Tag,'--repo',$Repository,'--title',('Chromo '+$Manifest.application_version+' / Chromium '+$Manifest.version),'--notes-file',$Notes)
if($Manifest.recipe_commit) { $Arguments += @('--target',$Manifest.recipe_commit) }
$Arguments += @($Files | ForEach-Object { Join-Path $Directory $_ })
& $GitHubCli @Arguments
if($LASTEXITCODE -ne 0) { throw 'GitHub release publication failed' }
