# Definitions only. Dot-sourcing must not inspect, start or modify the runtime.
function Get-NeoDedicatedBrowser {
    param([string]$Executable,[string]$Profile)
    @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object {
        $_.ExecutablePath -eq $Executable -and $_.CommandLine -and
        $_.CommandLine -match ('(?:^|\s)--user-data-dir=(?:"'+[regex]::Escape($Profile)+'"|'+[regex]::Escape($Profile)+'(?=\s|$))') -and
        $_.CommandLine -notlike '*--type=*'
    })
}
function Test-NeoPublisherSignature {
    param($Signature,[string]$Publisher)
    $Signature.Status -eq 'Valid' -and $Signature.SignerCertificate.GetNameInfo('SimpleName',$false) -eq $Publisher
}
function Read-Rpc {
    param($Reply,[int]$Id)
    $content=$Reply.Content
    if($content -is [byte[]]){$content=[Text.Encoding]::UTF8.GetString($content)}
    if($content.TrimStart().StartsWith('{')){return $content | ConvertFrom-Json}
    foreach($line in ($content -split '\r?\n')){
        if($line.StartsWith('data:')){
            $data=$line.Substring(5).Trim()
            if($data.StartsWith('{')){
                $message=$data | ConvertFrom-Json
                if($message.id -eq $Id){return $message}
            }
        }
    }
    throw 'MCP response did not contain a matching RPC result'
}
function Test-NeoEndpointIdentity {
    param([string]$Endpoint)
    $OutputEncoding=[Text.UTF8Encoding]::new($false)
    $json=$plan | ConvertTo-Json -Depth 12 -Compress
    $raw=$json | & $plan.python_executable -B -X utf8 (Join-Path $PSScriptRoot 'neo_endpoint_identity.py') --endpoint $Endpoint
    try {$identity=($raw -join "`n") | ConvertFrom-Json} catch {$script:mcpReadinessError='Endpoint identity helper failed';return $false}
    if(-not $identity.verified){$script:mcpReadinessError=$identity.reason;return $false}
    return $true
}
function Invoke-NeoOwnedWebRequest {
    param([switch]$UseBasicParsing,[string]$Uri,[string]$Method,$Headers,[string]$ContentType,[string]$Body,[int]$TimeoutSec)
    if(-not(Test-NeoEndpointIdentity $Uri)){throw "Unverified Neo endpoint: $script:mcpReadinessError"}
    Invoke-WebRequest @PSBoundParameters
}
function Test-McpEndpointReady {
    param([string]$Endpoint)
    $script:mcpReadinessError='MCP initialize/tools response was incomplete'
    $session=$null
    $headers=@{Accept='application/json, text/event-stream'}
    try {
        $body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"workspace-neo-readiness","version":"1"}}}'
        $reply=Invoke-NeoOwnedWebRequest -UseBasicParsing -Uri $Endpoint -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 2
        $session=$reply.Headers['Mcp-Session-Id'] | Select-Object -First 1
        $result=Read-Rpc $reply 1
        if(-not $result.result.protocolVersion){return $false}
        if($session){$headers['Mcp-Session-Id']=$session}
        $headers['MCP-Protocol-Version']=$result.result.protocolVersion
        Invoke-NeoOwnedWebRequest -UseBasicParsing -Uri $Endpoint -Method Post -Headers $headers -ContentType 'application/json' -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' -TimeoutSec 2 | Out-Null
        $reply=Invoke-NeoOwnedWebRequest -UseBasicParsing -Uri $Endpoint -Method Post -Headers $headers -ContentType 'application/json' -Body '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' -TimeoutSec 2
        $result=Read-Rpc $reply 2
        return @($result.result.tools | Where-Object {$_.name -eq 'run'}).Count -eq 1
    } catch {$script:mcpReadinessError=$_.Exception.Message;return $false}
    finally {
        if($session){try {Invoke-NeoOwnedWebRequest -UseBasicParsing -Uri $Endpoint -Method Delete -Headers $headers -TimeoutSec 2 | Out-Null}catch {}}
    }
}
function Test-McpReady {
    $script:mcpEffectiveEndpoint=$null
    if(Test-McpEndpointReady $plan.mcp_endpoint){$script:mcpEffectiveEndpoint=$plan.mcp_endpoint;return $true}
    return $false
}
function Assert-NeoPortsAvailable {
    param([int[]]$Ports)
    foreach($port in $Ports){
        $socket=New-Object Net.Sockets.Socket([Net.Sockets.AddressFamily]::InterNetwork,[Net.Sockets.SocketType]::Stream,[Net.Sockets.ProtocolType]::Tcp)
        try {
            $socket.ExclusiveAddressUse=$true
            $socket.Bind((New-Object Net.IPEndPoint([Net.IPAddress]::Any,$port)))
        } catch {throw "Dedicated Neo port $port cannot bind (listener or outbound reservation); no browser was started: $($_.Exception.Message)"}
        finally {$socket.Dispose()}
    }
}
function Write-JsonAtomic {
    param([string]$Path,$Value)
    $json=$Value | ConvertTo-Json -Depth 12 -Compress
    $code='import json,sys; sys.path.insert(0,sys.argv[2]); from workspace_storage import atomic_write_json; atomic_write_json(sys.argv[1],json.load(sys.stdin),backup_count=0)'
    $json | & $plan.python_executable -B -X utf8 -c $code $Path $PSScriptRoot
    if($LASTEXITCODE -ne 0){throw 'Atomic runtime state write failed'}
}
