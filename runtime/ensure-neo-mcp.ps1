[CmdletBinding()]
param([switch]$Check,[switch]$StopOwnedRecovery,[switch]$WaitForManagedStartup)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'neo-runtime-common.ps1')
$plan=Get-Content -Encoding UTF8 (Join-Path $PSScriptRoot 'runtime-plan.json') -Raw | ConvertFrom-Json
$proxyPort=([Uri]$plan.mcp_endpoint).Port
$cdpPort=[int]$plan.cdp_port
$backendPort=[int]$plan.backend_port
if($plan.session_idle_ms){$env:CLAW_SESSION_IDLE_MS=[string][long]$plan.session_idle_ms}
$state=$plan.server_state_root.Replace('/','\')
$profile=$plan.profile_root.Replace('/','\')
$recordPath=Join-Path $state 'recovery-server-process.json'
$browserExe=Join-Path $plan.proposed_runtime_root 'Chrome-bin\chrome.exe'
function Get-Browser {
    Get-NeoDedicatedBrowser -Executable $browserExe -Profile $profile
}
function Get-OwnedServer {
    if(-not(Test-Path -LiteralPath $recordPath)){return}
    $record=Get-Content -Encoding UTF8 -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.server.ProcessId)"
    # PID reuse proves the recorded process has ended; never manage its new owner.
    if($process -and $process.CreationDate.ToUniversalTime() -ne ([DateTime]$record.server.CreationDateUtc).ToUniversalTime()){return}
    if($process -and $process.ExecutablePath -eq $record.server.ExecutablePath -and
       $process.CommandLine -eq $record.server.CommandLine -and
       $process.ParentProcessId -eq $record.server.ParentProcessId -and
       $process.CreationDate.ToUniversalTime() -eq ([DateTime]$record.server.CreationDateUtc).ToUniversalTime()){return $process}
    if($process){throw 'Recovery process identity changed; refusing to manage it'}
}
function Stop-Recovery {
    $server=Get-OwnedServer
    if(-not $server){return}
    if(@(Get-Browser).Count){throw 'Browser is still running; preserve its recovery server'}
    $listeners=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -eq $proxyPort -and $_.OwningProcess -eq $server.ProcessId -and $_.LocalAddress -eq '127.0.0.1'})
    if($listeners.Count -ne 1){throw 'Recorded recovery server does not own the shutdown endpoint'}
    Invoke-RestMethod -Uri "http://127.0.0.1:$proxyPort/system/shutdown" -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    while(Get-OwnedServer){
        if([DateTime]::UtcNow -gt $deadline){throw 'Recovery server did not stop normally'}
        Start-Sleep -Milliseconds 200
    }
}
function Stop-OrphanedManagedServer {
    # A crashed browser may leave its managed server holding $backendPort/server.lock.
    # Only the exact child of our recorded launch may be recovered here.
    if(@(Get-Browser).Count){throw 'Browser is still running; preserve its managed server'}
    $launchPath=Join-Path (Split-Path $state -Parent) 'evidence\browser-launch.json'
    if(-not(Test-Path -LiteralPath $launchPath)){return}
    $launch=Get-Content -Encoding UTF8 -LiteralPath $launchPath -Raw | ConvertFrom-Json
    if(Get-CimInstance Win32_Process -Filter "ProcessId=$($launch.ProcessId)"){
        throw 'Recorded browser PID is still present; refusing orphan recovery'
    }
    $override=$plan.server_override
    if(-not $override.enabled){return}
    $serverExe=$override.executable.Replace('/','\')
    $expectedCommand='"'+$serverExe+'" --config="'+(Join-Path $profile '.browseros\config.json')+'"'
    $servers=@(Get-CimInstance Win32_Process -Filter "Name='browseros-claw-server.exe'" | Where-Object {
        $_.ParentProcessId -eq $launch.ProcessId -and $_.ExecutablePath -eq $serverExe -and $_.CommandLine -eq $expectedCommand
    })
    if(-not $servers.Count){return}
    # A completed version switch has no child to stop; its older launch record
    # must not block a new browser. Validate before any actual orphan mutation.
    $expectedBrowser='"'+$browserExe+'" --user-data-dir='+$profile+' '
    if(-not $launch.CommandLine.StartsWith($expectedBrowser)){throw 'Recorded browser identity is invalid'}
    if($servers.Count -ne 1){throw 'Ambiguous managed server identity'}
    $server=$servers[0]
    if((Get-FileHash -LiteralPath $serverExe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $override.sha256){throw 'Orphan server binary identity mismatch'}
    $owner=Invoke-CimMethod -InputObject $server -MethodName GetOwnerSid
    if($owner.ReturnValue -ne 0 -or $owner.Sid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or
       $server.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId){throw 'Orphan server user/session mismatch'}
    $listeners=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -in $proxyPort,$cdpPort,$backendPort})
    if($listeners.Count -ne 1 -or $listeners[0].LocalPort -ne $backendPort -or
       $listeners[0].LocalAddress -ne '127.0.0.1' -or $listeners[0].OwningProcess -ne $server.ProcessId){throw 'Unexpected orphan endpoint ownership'}
    # Recheck immutable process identity and browser absence immediately before shutdown.
    $current=Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ProcessId)"
    if(-not $current -or $current.CreationDate -ne $server.CreationDate -or
       $current.CommandLine -ne $server.CommandLine -or $current.ExecutablePath -ne $server.ExecutablePath -or
       $current.ParentProcessId -ne $server.ParentProcessId -or @(Get-Browser).Count -or
       (Get-CimInstance Win32_Process -Filter "ProcessId=$($launch.ProcessId)")){throw 'Orphan identity changed before shutdown'}
    Invoke-RestMethod -Uri "http://127.0.0.1:$backendPort/system/shutdown" -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    do {
        $current=Get-CimInstance Win32_Process -Filter "ProcessId=$($server.ProcessId)"
        if(-not $current -or $current.CreationDate -ne $server.CreationDate){return}
        if([DateTime]::UtcNow -gt $deadline){throw 'Orphan managed server did not stop normally'}
        Start-Sleep -Milliseconds 200
    } while($true)
}
if($StopOwnedRecovery){Stop-Recovery; Stop-OrphanedManagedServer; [pscustomobject]@{recoveryServerStopped=$true};return}
$browser=@(Get-Browser)
$owned=Get-OwnedServer
if($Check){
    [pscustomobject]@{mcpReady=Test-McpReady;effectiveMcpEndpoint=$script:mcpEffectiveEndpoint;recoveryServerPids=@($owned.ProcessId);mode=if($owned){'WORKSPACE_DIRECT_RECOVERY'}else{'CHROMIUM_MANAGED'}}
    return
}
if($browser.Count -ne 1){throw 'Exactly one dedicated browser is required for live MCP recovery'}
if(Test-McpReady){[pscustomobject]@{mcpReady=$true;recoveryServerPids=@($owned.ProcessId);recovered=$false};return}
# Cold startup is asynchronous. Do not interpret its temporary empty process
# list or locked profile as a stopped manager, and do not launch a competitor.
if($WaitForManagedStartup -and $plan.runtime_mode -ne 'DIRECT_LOOPBACK'){
    $deadline=[DateTime]::UtcNow.AddSeconds(20)
    do {
        if(Test-McpReady){[pscustomobject]@{mcpReady=$true;recovered=$false};return}
        if([DateTime]::UtcNow -ge $deadline){throw "Cold neo startup did not become MCP-ready: $script:mcpReadinessError"}
        Start-Sleep -Milliseconds 300
    } while($true)
}
$deadline=[DateTime]::UtcNow.AddSeconds(20)
do {
    $managed=@(Get-CimInstance Win32_Process -Filter "Name='browseros-claw-server.exe'" | Where-Object {$_.ParentProcessId -eq $browser[0].ProcessId})
    if(-not $managed.Count -and -not $owned){break}
    if(Test-McpReady){[pscustomobject]@{mcpReady=$true;recoveryServerPids=@($owned.ProcessId);recovered=$false};return}
    if([DateTime]::UtcNow -gt $deadline){throw "Existing neo server is not MCP-ready; no competing server was started: $script:mcpReadinessError"}
    Start-Sleep -Milliseconds 300
} while($true)
$override=$plan.server_override
if(-not $override.enabled -or (Get-FileHash -LiteralPath $override.executable -Algorithm SHA256).Hash.ToLowerInvariant() -ne $override.sha256){throw 'Verified recovery binary is unavailable'}
$listeners=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -in $proxyPort,$backendPort})
if($listeners.Count){throw 'MCP/backend port has an existing listener; refusing to replace it'}
$configPath=Join-Path $profile '.browseros\config.json'
$config=Get-Content -Encoding UTF8 -LiteralPath $configPath -Raw | ConvertFrom-Json
if($config.ports.proxy -ne $proxyPort -or $config.ports.server -ne $backendPort -or $config.ports.cdp -ne $cdpPort){throw 'Unexpected dedicated neo port layout'}
$cdpOwner=@(Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -eq $cdpPort -and $_.OwningProcess -eq $browser[0].ProcessId -and $_.LocalAddress -eq '127.0.0.1'})
if($cdpOwner.Count -ne 1){throw 'CDP port is not owned by the verified dedicated browser'}
$cdp=Invoke-RestMethod -Uri "http://127.0.0.1:$cdpPort/json/version" -TimeoutSec 3
if(-not $cdp.webSocketDebuggerUrl){throw 'Existing browser CDP is unavailable'}
# A stopped Chromium manager releases this file. Never compete with an active launch.
$profileLock=Join-Path $profile '.browseros\server.lock'
$lock=[IO.File]::Open($profileLock,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
try {
    $config.ports.server=$proxyPort
    $config.directories.resources=$override.resources_root.Replace('/','\')
    $recoveryConfig=Join-Path $state 'recovery-server-config.json'
    Write-JsonAtomic $recoveryConfig $config
    $env:BROWSEROS_DIR=$state;$env:BROWSERCLAW_DIR=$state;$env:TEMP=$state;$env:TMP=$state;$env:TMPDIR=$state
    $env:WORKSPACE_NEO_MANUAL_TAB_CLEANUP='1';$env:WORKSPACE_NEO_PRESERVE_AGENT_CONFIG='1';$env:WORKSPACE_NEO_STATEFUL_PREFLIGHT_COMPAT='1';$env:WORKSPACE_NEO_DIRECT_RECOVERY='1'
    $stamp=[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssffff')
    $stderr=Join-Path $state "logs\recovery-server-$stamp.stderr.log"
    $p=Start-Process -FilePath $override.executable -ArgumentList @(('--config="{0}"' -f $recoveryConfig)) -WorkingDirectory $state -WindowStyle Hidden -RedirectStandardError $stderr -PassThru
    $identity=Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)"
    if(-not $identity){throw 'Recovery server exited before its identity could be recorded'}
    Write-JsonAtomic $recordPath ([pscustomobject]@{
        server=[pscustomobject]@{ProcessId=$identity.ProcessId;ParentProcessId=$identity.ParentProcessId;CreationDateUtc=$identity.CreationDate.ToUniversalTime().ToString('o');ExecutablePath=$identity.ExecutablePath;CommandLine=$identity.CommandLine}
        browser=[pscustomobject]@{ProcessId=$browser[0].ProcessId;CreationDateUtc=$browser[0].CreationDate.ToUniversalTime().ToString('o')}
        sha256=$override.sha256;mode='WORKSPACE_DIRECT_RECOVERY';intent='Persistent while this dedicated browser is needed';stderr=$stderr
    })
} finally {$lock.Dispose()}
$deadline=[DateTime]::UtcNow.AddSeconds(15)
while(-not(Test-McpReady)){
    if(-not(Get-OwnedServer) -or [DateTime]::UtcNow -gt $deadline){throw 'Recovery server failed MCP readiness; inspect recorded stderr and exact process'}
    Start-Sleep -Milliseconds 300
}
[pscustomobject]@{mcpReady=$true;recovered=$true;recoveryServerPids=@($p.Id);mode='WORKSPACE_DIRECT_RECOVERY'}
