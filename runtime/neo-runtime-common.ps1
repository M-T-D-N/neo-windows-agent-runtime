# Definitions only. Dot-sourcing must not inspect, start or modify the runtime.
function Get-NeoCommandLineArguments {
    param([string]$CommandLine)
    if(-not ('WorkspaceNeo.NativeArguments' -as [type])){
        # In-memory P/Invoke, with no Add-Type compiler/temp files. Use Windows'
        # own quote/backslash rules instead of inspecting raw command-line text.
        $assembly=[Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly([Reflection.AssemblyName]::new('WorkspaceNeo.Arguments'),[Reflection.Emit.AssemblyBuilderAccess]::Run)
        $module=$assembly.DefineDynamicModule('Native')
        $type=$module.DefineType('WorkspaceNeo.NativeArguments',[Reflection.TypeAttributes]'Public,Sealed,Abstract')
        $method=$type.DefinePInvokeMethod('CommandLineToArgvW','shell32.dll',[Reflection.MethodAttributes]'Public,Static,PinvokeImpl',[Reflection.CallingConventions]::Standard,[IntPtr],@([string],[int].MakeByRefType()),[Runtime.InteropServices.CallingConvention]::Winapi,[Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
        $method=$type.DefinePInvokeMethod('LocalFree','kernel32.dll',[Reflection.MethodAttributes]'Public,Static,PinvokeImpl',[Reflection.CallingConventions]::Standard,[IntPtr],@([IntPtr]),[Runtime.InteropServices.CallingConvention]::Winapi,[Runtime.InteropServices.CharSet]::Unicode)
        $method.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
        [void]$type.CreateType()
    }
    $count=0
    $memory=[WorkspaceNeo.NativeArguments]::CommandLineToArgvW($CommandLine,[ref]$count)
    if($memory -eq [IntPtr]::Zero){throw 'Unable to parse native process arguments'}
    try {
        for($i=0;$i -lt $count;$i++){
            [Runtime.InteropServices.Marshal]::PtrToStringUni([Runtime.InteropServices.Marshal]::ReadIntPtr($memory,$i*[IntPtr]::Size))
        }
    } finally {[void][WorkspaceNeo.NativeArguments]::LocalFree($memory)}
}
function Test-NeoStableArgument {
    param([string]$Value)
    if([string]::IsNullOrEmpty($Value)){return $false}
    $end=$Value.Length-1
    if($end -gt 0 -and [char]::IsLowSurrogate($Value[$end]) -and [char]::IsHighSurrogate($Value[$end-1])){$end--}
    foreach($index in @(0,$end)){
        if([char]::IsWhiteSpace($Value,$index) -or
            [Globalization.CharUnicodeInfo]::GetUnicodeCategory($Value,$index).ToString() -match 'Separator$|^(Control|Format|Surrogate|PrivateUse|OtherNotAssigned)$'){return $false}
    }
    return $true
}
function Get-NeoDedicatedBrowser {
    param([string]$Executable,[string]$Profile)
    $found=@()
    $unknown='Unqualified Neo browser root is already running; preserve existing processes'
    foreach($process in @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'")){
        if($process.ExecutablePath -ne $Executable){continue}
        $raw=$process.CommandLine
        # Only the raw boundary check tolerates trailing delimiters. Parse the
        # original string, so an unmatched quote cannot hide argument whitespace.
        $rawEdges=$raw
        if($raw){$rawEdges=$raw.TrimEnd([char[]]" `t")}
        if(-not(Test-NeoStableArgument $rawEdges)){throw $unknown}
        $arguments=@(Get-NeoCommandLineArguments $raw)
        if(@($arguments | Where-Object {-not(Test-NeoStableArgument $_)}).Count){throw $unknown}
        $arguments=@($arguments | Select-Object -Skip 1)
        if(@($arguments | Where-Object {$_ -eq '--' -or $_ -match '^(?:--|-|/)single-argument(?:=|$)'}).Count){throw $unknown}
        $profiles=@($arguments | Where-Object {$_ -match '^(?:--|-|/)user-data-dir(?:=|$)'})
        $types=@($arguments | Where-Object {$_ -match '^(?:--|-|/)type(?:=|$)'})
        if($types.Count -eq 1 -and $types[0].StartsWith('--type=',[StringComparison]::Ordinal) -and $types[0].Length -gt 7){continue}
        # Count every recognized alias/casing; only our canonical one-switch
        # spelling qualifies. Noncanonical input is preserved, never adopted.
        if($types.Count -or $profiles.Count -ne 1 -or -not $profiles[0].StartsWith('--user-data-dir=',[StringComparison]::Ordinal)){throw $unknown}
        $value=$profiles[0].Substring(16).Replace('/','\')
        if($value -notmatch '^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)' -or [IO.Path]::GetFullPath($value) -ne [IO.Path]::GetFullPath($Profile.Replace('/','\'))){throw $unknown}
        $found+=$process
    }
    $found
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
    $script:mcpIdentity=$null
    try {$identity=($raw -join "`n") | ConvertFrom-Json} catch {$script:mcpReadinessError='Endpoint identity helper failed';return $false}
    $script:mcpIdentity=$identity
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
