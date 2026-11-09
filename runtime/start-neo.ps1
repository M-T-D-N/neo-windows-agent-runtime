[CmdletBinding()]
param([switch]$Check,[switch]$RestoreSession,[switch]$StopOwnedRecoveryServer,[switch]$RuntimeWorker)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$OutputEncoding=[Console]::OutputEncoding
. (Join-Path $PSScriptRoot 'neo-runtime-common.ps1')
$plan=Get-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'runtime-plan.json') -Raw | ConvertFrom-Json
$proxyPort=([Uri]$plan.mcp_endpoint).Port
$cdpPort=[int]$plan.cdp_port
$backendPort=[int]$plan.backend_port
if($plan.session_idle_ms){$env:CLAW_SESSION_IDLE_MS=[string][long]$plan.session_idle_ms}
$exe=Join-Path $plan.proposed_runtime_root 'Chrome-bin\chrome.exe'
$sig=Get-AuthenticodeSignature $exe
if(-not (Test-NeoPublisherSignature $sig $plan.authenticode_publisher)){throw 'Invalid browser signature'}
function Get-DedicatedBrowser {
    Get-NeoDedicatedBrowser -Executable $exe -Profile $plan.profile_root.Replace('/','\')
}
function Write-RuntimeStatus {
    param($Running)
    $runtime=Join-Path $plan.server_state_root 'runtime.json'
    $override=$plan.server_override
    $servers=@(Get-CimInstance Win32_Process -Filter "Name='browseros-claw-server.exe'" | Where-Object {
        $_.ParentProcessId -in @($Running.ProcessId)
    })
    $health=& (Join-Path $PSScriptRoot 'ensure-neo-mcp.ps1') -Check
    foreach($id in @($health.recoveryServerPids | Where-Object {$_})){
        $servers+=@(Get-CimInstance Win32_Process -Filter "ProcessId=$id")
    }
    $overrideActive=$false
    if($override -and $override.enabled){
        $expected=$override.executable.Replace('/','\')
        $overrideActive=@($servers | Where-Object {$_.ExecutablePath -eq $expected}).Count -gt 0
    }
    [pscustomobject]@{mcpReady=$health.mcpReady;effectiveMcpEndpoint=$health.effectiveMcpEndpoint;serverMode=$health.mode;serverOverrideBinaryActive=$overrideActive;serverPids=@($servers.ProcessId);running=$null -ne $Running -and @($Running).Count -gt 0;pids=@($Running.ProcessId);runtime=if(Test-Path $runtime){Get-Content -Encoding UTF8 $runtime -Raw}else{$null};profile=$plan.profile_root;version=$plan.version}
}
if($Check){Write-RuntimeStatus (Get-DedicatedBrowser);return}
# Add-Type's compiler intermediates belong to this runtime's D state as well.
New-Item -ItemType Directory -Path $plan.server_state_root -Force | Out-Null
$env:TEMP=$plan.server_state_root;$env:TMP=$plan.server_state_root;$env:TMPDIR=$plan.server_state_root
# The fixed local WMI worker runs as the caller in the same desktop session.
# It owns persistent runtime activation independently of a disposable MCP Job.
# This does not change Job limits, ACLs, account privileges or scheduled tasks.
if(-not ('WorkspaceNeo.Lifetime' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace WorkspaceNeo {
    public static class Lifetime {
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool inside);
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")]
        public static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError=true)]
        public static extern bool GetExitCodeProcess(IntPtr handle, out uint code);
        [DllImport("kernel32.dll")]
        public static extern bool CloseHandle(IntPtr handle);
    }
}
"@
}
function Start-PersistentRuntimeWorker {
    $launcher=Join-Path $PSScriptRoot 'start-neo.ps1'
    $shell=Join-Path ([Environment]::GetFolderPath('Windows')) 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if($launcher.Contains('"') -or $shell.Contains('"')){throw 'Invalid runtime activation path'}
    $command='"'+$shell+'" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$launcher+'" -RuntimeWorker'
    if($RestoreSession){$command+=' -RestoreSession'}
    $startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
    $created=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine=$command;CurrentDirectory=$plan.server_state_root;ProcessStartupInformation=$startup
    }
    if($created.ReturnValue -ne 0){throw "Local runtime activation failed: $($created.ReturnValue)"}
    $handle=[WorkspaceNeo.Lifetime]::OpenProcess(0x100000 -bor 0x1000,$false,$created.ProcessId)
    if($handle -eq [IntPtr]::Zero){throw 'Unable to observe runtime activation worker'}
    try {
        $worker=Get-CimInstance Win32_Process -Filter "ProcessId=$($created.ProcessId)"
        if(-not $worker -or $worker.CommandLine -ne $command -or
           $worker.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId){
            throw 'Runtime activation worker identity/session mismatch'
        }
        $owner=Invoke-CimMethod -InputObject $worker -MethodName GetOwnerSid
        if($owner.ReturnValue -ne 0 -or $owner.Sid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value){
            throw 'Runtime activation worker user mismatch'
        }
        $evidence=Join-Path (Split-Path $plan.server_state_root -Parent) 'evidence'
        New-Item -ItemType Directory -Path $evidence -Force | Out-Null
        $worker | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine,SessionId |
            ConvertTo-Json | Set-Content (Join-Path $evidence 'activation-worker.json')
        $wait=[WorkspaceNeo.Lifetime]::WaitForSingleObject($handle,45000)
        if($wait -ne 0){throw "Runtime activation worker did not finish: $wait; PID $($created.ProcessId)"}
        $code=[uint32]0
        if(-not [WorkspaceNeo.Lifetime]::GetExitCodeProcess($handle,[ref]$code) -or $code -ne 0){
            $workerLog=Join-Path $plan.server_state_root "logs\activation-worker-$($created.ProcessId).log"
            $detail=if(Test-Path -LiteralPath $workerLog){(Get-Content -Encoding UTF8 -LiteralPath $workerLog -Tail 25) -join "`n"}else{'Worker diagnostic was not created'}
            throw "Runtime activation worker failed: $code; PID $($created.ProcessId); diagnostic: $workerLog`n$detail"
        }
    } finally {[void][WorkspaceNeo.Lifetime]::CloseHandle($handle)}
    $running=@(Get-DedicatedBrowser)
    $status=Write-RuntimeStatus $running
    if(-not $status.running -or -not $status.mcpReady){throw 'Activated runtime is not MCP ready'}
    $status
}
# A native MCP client may be created for several conversations simultaneously.
# Check and launch under the same local lock; do not start competing profile owners.
$mutex=[Threading.Mutex]::new($false,('Local\NeoAgent.' + $plan.installation_id))
$locked=$false
$workerTranscript=$false
if($RuntimeWorker){
    $workerLog=Join-Path $plan.server_state_root "logs\activation-worker-$PID.log"
    New-Item -ItemType Directory -Path (Split-Path $workerLog -Parent) -Force | Out-Null
    Start-Transcript -LiteralPath $workerLog -Force | Out-Null
    $workerTranscript=$true
}
try {
    try {$locked=$mutex.WaitOne(15000)} catch [Threading.AbandonedMutexException] {$locked=$true}
    if(-not $locked){throw 'Dedicated neo launcher is already busy'}
    if($StopOwnedRecoveryServer){& (Join-Path $PSScriptRoot 'ensure-neo-mcp.ps1') -StopOwnedRecovery;return}
    $insideJob=$false
    if(-not [WorkspaceNeo.Lifetime]::IsProcessInJob([Diagnostics.Process]::GetCurrentProcess().Handle,[IntPtr]::Zero,[ref]$insideJob)){
        throw 'Unable to determine runtime lifetime ownership'
    }
    if($insideJob -and -not $RuntimeWorker){
        # The worker takes the same mutex and rechecks browser/port ownership.
        $mutex.ReleaseMutex();$locked=$false
        Start-PersistentRuntimeWorker;return
    }
    $running=@(Get-DedicatedBrowser)
    if($running.Count){
        & (Join-Path $PSScriptRoot 'ensure-neo-mcp.ps1') | Out-Null
        Write-RuntimeStatus $running;return
    }
    & (Join-Path $PSScriptRoot 'ensure-neo-mcp.ps1') -StopOwnedRecovery | Out-Null
    $mcpPort=([Uri]$plan.mcp_endpoint).Port
    # A global MCP endpoint must stay stable across persisted Chromium port prefs.
    Assert-NeoPortsAvailable @($mcpPort,$cdpPort,$backendPort)
    $state=$plan.server_state_root
    New-Item -ItemType Directory -Path $state,$plan.profile_root -Force | Out-Null
    $marker=Join-Path $state 'first-run-connect.json'
    if(-not(Test-Path $marker)){[IO.File]::WriteAllText($marker,'{"firstRunConnectDone":true}')}
    # MCP tool output reads BROWSEROS_DIR independently of BrowserClaw state.
    $env:BROWSEROS_DIR=$state;$env:BROWSERCLAW_DIR=$state;$env:TEMP=$state;$env:TMP=$state;$env:TMPDIR=$state
    $profile=$plan.profile_root.Replace('/','\')
    $arguments=@("--user-data-dir=`"$profile`"",'--no-first-run','--no-default-browser-check',"--browseros-proxy-port=$mcpPort","--browseros-cdp-port=$cdpPort","--browseros-server-port=$backendPort")
    $logRoot=Join-Path $state 'logs'
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
    $browserLog=Join-Path $logRoot ("chromium-server-"+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssffff')+".log")
    # Chromium --enable-logging allocates a visible console on Windows,
    # including its own --restart path. Keep startup errors in the D log.
    $arguments+='--restore-last-session'
    # The installer updater targets the C installation, not this D payload.
    # Updates use update-runtime.py + prepare-runtime.ps1 instead.
    $arguments+='--disable-updates'
    # The upstream proxy binds all interfaces. Use the verified loopback server.
    $arguments+='--disable-browseros-server'
    $override=$plan.server_override
    if($override -and $override.enabled){
        $serverExe=$override.executable.Replace('/','\')
        if((Get-FileHash -LiteralPath $serverExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $override.sha256){throw 'neo server override identity mismatch'}
        # GetBestAvailable prefers bundled on equal version. A newer cached download wins.
        $versions=Join-Path $plan.profile_root '.browseros\BrowserClawServer\versions'
        if(Test-Path -LiteralPath $versions){
            foreach($entry in Get-ChildItem -LiteralPath $versions -Directory){
                $cachedVersion=$null
                if([version]::TryParse($entry.Name,[ref]$cachedVersion) -and $cachedVersion -gt [version]$override.version){
                    throw 'A newer cached neo server would bypass the verified override; review it before launch'
                }
            }
        }
        $env:WORKSPACE_NEO_MANUAL_TAB_CLEANUP='1'
        $env:WORKSPACE_NEO_PRESERVE_AGENT_CONFIG='1'
        $env:WORKSPACE_NEO_STATEFUL_PREFLIGHT_COMPAT='1'
        $arguments+="--browseros-server-resources-dir=`"$($override.resources_root.Replace('/','\'))`""
        $arguments+='--disable-browseros-server-updater'
    }
    $env:WORKSPACE_NEO_DIRECT_RECOVERY=$null
    # Hide only the activation worker; the agent browser must remain visible.
    $p=Start-Process $exe -ArgumentList $arguments -WorkingDirectory $state -WindowStyle Normal -RedirectStandardError $browserLog -PassThru
    $evidence=Join-Path (Split-Path $plan.server_state_root -Parent) 'evidence'
    New-Item -ItemType Directory -Path $evidence -Force | Out-Null
    Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json | Set-Content (Join-Path $evidence 'browser-launch.json')
    $deadline=[DateTime]::UtcNow.AddSeconds(5)
    do {
        $running=@(Get-DedicatedBrowser)
        if($running.Count){break}
        Start-Sleep -Milliseconds 100
    } while([DateTime]::UtcNow -lt $deadline)
    if(-not $running.Count){throw 'neo launch returned without a dedicated browser process'}
    & (Join-Path $PSScriptRoot 'ensure-neo-mcp.ps1') -WaitForManagedStartup | Out-Null
    [pscustomobject]@{started=$p.Id;profile=$plan.profile_root;serverState=$state;intent='Persistent dedicated agent browser'}
} catch {
    if($workerTranscript){Write-Host ($_ | Out-String)}
    throw
} finally {
    if($workerTranscript){Stop-Transcript | Out-Null}
    if($locked){$mutex.ReleaseMutex()}
    $mutex.Dispose()
}
