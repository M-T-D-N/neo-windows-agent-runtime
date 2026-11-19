[CmdletBinding()]
param(
    [switch]$CheckUpdate,
    [switch]$ProbeMcp,
    [string]$IncidentTime,
    [uint32[]]$BrowserPid
)
$ErrorActionPreference = 'Stop'
$planPath = Join-Path $PSScriptRoot 'runtime-plan.json'
$plan = Get-Content -Encoding UTF8 -LiteralPath $planPath -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'neo-runtime-common.ps1')
if ([bool]$IncidentTime -ne [bool]$BrowserPid) {
    throw 'Specify both IncidentTime (with UTC offset) and BrowserPid for audit lookup.'
}

$proxyPort = ([Uri]$plan.mcp_endpoint).Port
$cdpPort = if ($plan.cdp_port) { [int]$plan.cdp_port } else { 9111 }
$backendPort = if ($plan.backend_port) { [int]$plan.backend_port } else { 9210 }
$status = $null
$statusError = $null
$identity = $null
$browsers = @()
$python = $plan.python_executable
try {
    $browserExe = Join-Path $plan.proposed_runtime_root 'Chrome-bin\chrome.exe'
    $browsers = @(Get-NeoDedicatedBrowser -Executable $browserExe -Profile $plan.profile_root)
    if ($plan.runtime_mode -eq 'DIRECT_LOOPBACK') {
        $OutputEncoding = [Text.UTF8Encoding]::new($false)
        $planJson = $plan | ConvertTo-Json -Depth 12 -Compress
        $raw = $planJson | & $python -B -X utf8 (Join-Path $PSScriptRoot 'neo_endpoint_identity.py') --endpoint $plan.mcp_endpoint
        $identity = ($raw -join "`n") | ConvertFrom-Json
        if (-not $identity.code) { throw 'Endpoint identity lookup returned no result' }
    }
    if ($ProbeMcp) {
        $status = & (Join-Path $PSScriptRoot 'start-neo.ps1') -Check
    }
} catch {
    $statusError = $_.Exception.Message
}
$listenerError = $null
$listeners = @()
try {
    $listeners = @(Get-NetTCPConnection -State Listen | Where-Object {
        $_.LocalPort -in @($proxyPort,$cdpPort,$backendPort)
    } | Select-Object LocalAddress,LocalPort,OwningProcess)
} catch {
    $listenerError = $_.Exception.Message
}
$code = if ($identity.code -and -not $identity.verified) { $identity.code }
    elseif ($status.mcpReady) { 'READY' }
    elseif ($listenerError -or $statusError) { 'UNVERIFIED' }
    elseif (-not ($listeners.LocalPort -contains $proxyPort -or $listeners.LocalPort -contains $backendPort)) { 'MCP_NOT_LISTENING' }
    elseif ($browsers.Count -and $listeners.LocalPort -notcontains $cdpPort) { 'CDP_NOT_LISTENING' }
    elseif (-not $ProbeMcp) { 'MCP_LISTENER_PRESENT_NOT_PROBED' }
    else { 'MCP_NOT_READY' }
$browserProcesses = @($browsers | Select-Object ProcessId,ParentProcessId,
    @{Name='StartedUtc';Expression={$_.CreationDate.ToUniversalTime().ToString('o')}})

$registered = (Get-ItemProperty -LiteralPath 'HKCU:\Software\BrowserClaw' -Name pv -ErrorAction SilentlyContinue).pv
$registryMismatch = $null
if ($registered -and $plan.chromium_version) {
    $registryMismatch = $registered -ne $plan.chromium_version
}
$feed = $null
if ($CheckUpdate) {
    try {
        $raw = & $python -B -X utf8 (Join-Path $PSScriptRoot 'update-runtime.py') --plan $planPath
        if ($LASTEXITCODE -ne 0) { throw "Feed comparison failed with exit $LASTEXITCODE" }
        $feed = ($raw -join "`n") | ConvertFrom-Json
    } catch { $feed = @{status='unverified'; reason=$_.Exception.Message} }
}
$audit = if ($IncidentTime) {
    & (Join-Path $PSScriptRoot 'Get-NeoRestartEvents.ps1') -PlanPath $planPath -IncidentTime $IncidentTime -BrowserPid $BrowserPid |
        ConvertFrom-Json
}
[pscustomobject]@{
    checkedAtUtc = [DateTime]::UtcNow.ToString('o')
    browserVersion = $plan.version
    chromiumVersion = $plan.chromium_version
    serverVersion = $plan.server_override.version
    mcpReady = $status.mcpReady
    connectionCode = $code
    connectionIdentity = $identity
    connectionError = if ($statusError) { $statusError } else { $status.mcpReadinessError }
    mcpProbeRequested = [bool]$ProbeMcp
    mcpProbeCleanupErrors = @($status.mcpProbeCleanupErrors | Where-Object { $_ })
    browserProcesses = $browserProcesses
    serverPids = @(@($status.serverPids) + @($identity.mcpPid) | Where-Object { $_ } | Select-Object -Unique)
    listenerError = $listenerError
    listeners = $listeners
    windowsRegisteredChromium = $registered
    registryVersionMismatch = $registryMismatch
    feed = $feed
    restartAudit = $audit
} | ConvertTo-Json -Depth 10
