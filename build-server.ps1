[CmdletBinding()]
param([Parameter(Mandatory)][string]$Destination,[ValidateRange(1,16)][int]$Jobs=2,[switch]$PrepareOnly,[string]$BundleRoot)
$ErrorActionPreference='Stop'
if(-not $BundleRoot){$BundleRoot=$PSScriptRoot}
$pins=Get-Content -LiteralPath (Join-Path $BundleRoot 'pins.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$destinationPath=[IO.Path]::GetFullPath($Destination)
if(Test-Path -LiteralPath $destinationPath){throw 'Use a new owned build directory'}
foreach($patch in $pins.patches.PSObject.Properties){
    $file=Join-Path (Join-Path $BundleRoot 'patches') $patch.Name
    if((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $patch.Value){throw 'Patch identity mismatch'}
}
& git clone --no-checkout --filter=blob:none $pins.upstream_repository $destinationPath
if($LASTEXITCODE){throw 'Source clone failed'}
Push-Location $destinationPath
$previousTarget=$env:CARGO_TARGET_DIR
$previousTemp=$env:TEMP
$previousTmp=$env:TMP
$previousTmpdir=$env:TMPDIR
$previousEncodedFlags=$env:CARGO_ENCODED_RUSTFLAGS
try {
    & git sparse-checkout init --cone
    if($LASTEXITCODE){throw 'Sparse source preparation failed'}
    & git sparse-checkout set packages/browseros-agent
    if($LASTEXITCODE){throw 'Sparse source selection failed'}
    & git checkout --detach $pins.server_override.source_commit
    if($LASTEXITCODE){throw 'Pinned checkout failed'}
    foreach($name in @('neo-workspace-runtime.patch','neo-close-notice-0.0.65.patch')){
        & git apply --directory=packages/browseros-agent --check (Join-Path (Join-Path $BundleRoot 'patches') $name)
        if($LASTEXITCODE){throw 'Patch check failed'}
        & git apply --directory=packages/browseros-agent (Join-Path (Join-Path $BundleRoot 'patches') $name)
        if($LASTEXITCODE){throw 'Patch application failed'}
    }
    if($PrepareOnly){[pscustomobject]@{source=$destinationPath;commit=$pins.server_override.source_commit;patched=$true;built=$false}|ConvertTo-Json;return}
    $env:CARGO_TARGET_DIR=Join-Path $destinationPath 'build-target'
    $ownedTemp=Join-Path $destinationPath 'build-temp'
    New-Item -ItemType Directory -Path $ownedTemp | Out-Null
    $env:TEMP=$ownedTemp;$env:TMP=$ownedTemp;$env:TMPDIR=$ownedTemp
    if($env:RUSTFLAGS -and -not $previousEncodedFlags){throw 'Use CARGO_ENCODED_RUSTFLAGS for explicit additional flags'}
    $rustFlags=@()
    if($previousEncodedFlags){$rustFlags+=@($previousEncodedFlags -split [char]31)}
    $rustFlags+="--remap-path-prefix=$destinationPath=/src/neo"
    if($env:CARGO_HOME){$rustFlags+="--remap-path-prefix=$env:CARGO_HOME=/cargo"}
    if($env:USERPROFILE){$rustFlags+="--remap-path-prefix=$env:USERPROFILE=/user"}
    $env:CARGO_ENCODED_RUSTFLAGS=$rustFlags -join [char]31
    Push-Location (Join-Path $destinationPath 'packages/browseros-agent')
    try { & cargo build --locked --release --jobs $Jobs -p claw-server-rust --bin browseros-claw-server-rs }
    finally { Pop-Location }
    if($LASTEXITCODE){throw 'Server build failed'}
    $exe=Join-Path $destinationPath 'build-target/release/browseros-claw-server-rs.exe'
    $sha=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{path=$exe;sha256=$sha;matchesQualifiedBinary=($sha -eq $pins.server_override.sha256);note='A different build hash requires catalog regeneration and complete candidate qualification before changing the pin.'}|ConvertTo-Json
} finally {
    $env:CARGO_TARGET_DIR=$previousTarget
    $env:CARGO_ENCODED_RUSTFLAGS=$previousEncodedFlags
    $env:TEMP=$previousTemp;$env:TMP=$previousTmp;$env:TMPDIR=$previousTmpdir
    Pop-Location
}
