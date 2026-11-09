[CmdletBinding()]
param(
 [Parameter(Mandatory)][string]$Installer,
 [Parameter(Mandatory)][string]$ScratchRoot,
 [switch]$Execute,
 [string]$PlanPath
)
$ErrorActionPreference='Stop'
if (-not $PlanPath) { $PlanPath=Join-Path $PSScriptRoot 'runtime-plan.json' }
. (Join-Path $PSScriptRoot 'neo-runtime-common.ps1')
$plan=Get-Content -Encoding UTF8 -LiteralPath $PlanPath -Raw | ConvertFrom-Json
$installerPath=[IO.Path]::GetFullPath($Installer)
$scratchPath=[IO.Path]::GetFullPath($ScratchRoot).TrimEnd('\')
$allowedScratch=[IO.Path]::GetFullPath($plan.scratch_root).TrimEnd('\')+'\'
if (-not $scratchPath.StartsWith($allowedScratch, [StringComparison]::OrdinalIgnoreCase)) { throw 'Use an owned installation scratch subdirectory' }
foreach ($path in @($installerPath,$scratchPath,$plan.proposed_runtime_root)) {
 $component=[IO.Path]::GetFullPath($path)
 while ($component) {
  $item=Get-Item -LiteralPath $component -ErrorAction SilentlyContinue
  if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Reparse component: $component" }
  $parent=Split-Path -Parent $component
  if (-not $parent -or $parent -eq $component) { break }
  $component=$parent
 }
}
$signature=Get-AuthenticodeSignature -LiteralPath $installerPath
if (-not (Test-NeoPublisherSignature $signature $plan.authenticode_publisher)) { throw 'Installer signature/publisher mismatch' }
if ((Get-Item -LiteralPath $installerPath).Length -ne $plan.installer_bytes -or (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $plan.installer_sha256) { throw 'Installer pin mismatch' }
if (Test-Path -LiteralPath $plan.proposed_runtime_root) { throw 'Candidate already exists; inspect, do not overwrite' }
[pscustomobject]@{validated=$true;execute=[bool]$Execute;source=$installerPath;destination=$plan.proposed_runtime_root;mode='Intact payload extraction, no setup execution';runtimeVerified=$false} | ConvertTo-Json
if (-not $Execute) { return }
$stage=Join-Path $scratchPath ('neo-extract-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
$env:TEMP=$stage; $env:TMP=$stage; $env:TMPDIR=$stage
@"
import pefile, sys
from pathlib import Path
p=pefile.PE(sys.argv[1])
for kind in p.DIRECTORY_ENTRY_RESOURCE.entries:
 for item in kind.directory.entries:
  if str(item.name)=='CHROME.PACKED.7Z':
   r=item.directory.entries[0].data.struct
   with Path(sys.argv[2]).open('xb') as f: f.write(p.get_data(r.OffsetToData,r.Size))
"@ | & $plan.python_executable -B - $installerPath (Join-Path $stage 'chrome.packed.7z')
if ($LASTEXITCODE) { throw 'PE extraction failed' }
& tar -xf (Join-Path $stage 'chrome.packed.7z') -C $stage
if ($LASTEXITCODE) { throw 'Outer archive extraction failed' }
$names=@(& tar -tf (Join-Path $stage 'chrome.7z'))
if ($LASTEXITCODE -or -not $names.Count) { throw 'Archive listing failed' }
foreach ($name in $names) {
 if ($name -notmatch '^Chrome-bin/' -or $name -match '(^|[/\\])\.\.([/\\]|$)|^[A-Za-z]:|^[/\\]') { throw 'Unsafe archive entry' }
}
New-Item -ItemType Directory -Path $plan.proposed_runtime_root | Out-Null
& tar -xf (Join-Path $stage 'chrome.7z') -C $plan.proposed_runtime_root
if ($LASTEXITCODE) { throw "Extraction failed; preserve candidate and staging: $stage" }
$app=Join-Path $plan.proposed_runtime_root 'Chrome-bin\chrome.exe'
$appSignature=Get-AuthenticodeSignature -LiteralPath $app
if (-not (Test-NeoPublisherSignature $appSignature $plan.authenticode_publisher)) { throw 'Extracted browser signature mismatch' }
[pscustomobject]@{candidate=$plan.proposed_runtime_root;executable=$app;signatureStatus=[string]$appSignature.Status;productVersion=(Get-Item -LiteralPath $app).VersionInfo.ProductVersion;staging=$stage;runtimeVerified=$false;next='Validate first-run controls then launch dedicated D profile; no browser/server was started'} | ConvertTo-Json
