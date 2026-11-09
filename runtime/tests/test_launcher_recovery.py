import subprocess
import sys
from pathlib import Path
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows launcher")
class OrphanRecoveryTests(unittest.TestCase):
    def check(self, mode):
        source = Path(__file__).resolve().parents[1] / "ensure-neo-mcp.ps1"
        # Execute the production function with OS observations substituted.
        code = r"""
$proxyPort=9010;$cdpPort=9111;$backendPort=9210
$tree=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$null,[ref]$null)
$fn=$tree.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-OrphanedManagedServer'},$true)
Invoke-Expression $fn.Extent.Text
$state='D:\owned\server';$profile='D:\owned\profile';$browserExe='D:\owned\chrome.exe'
$plan=[pscustomobject]@{server_override=[pscustomobject]@{enabled=$true;executable='D:/owned/server.exe';sha256='abc'}}
$server=[pscustomobject]@{ProcessId=22;ParentProcessId=11;ExecutablePath='D:\owned\server.exe';CommandLine='"D:\owned\server.exe" --config="D:\owned\profile\.browseros\config.json"';CreationDate=[datetime]'2026-01-01';SessionId=[Diagnostics.Process]::GetCurrentProcess().SessionId}
$script:stopped=$false;$script:changed=$false
function Get-Browser { if('__MODE__' -eq 'live'){[pscustomobject]@{ProcessId=11}} }
function Test-Path {return $true}
function Get-Content {
 if('__MODE__' -eq 'upgraded'){return '{"ProcessId":11,"CommandLine":"\"D:\\owned\\old-chrome.exe\" --user-data-dir=D:\\owned\\profile "}'}
 return '{"ProcessId":11,"CommandLine":"\"D:\\owned\\chrome.exe\" --user-data-dir=D:\\owned\\profile "}'
}
function Get-FileHash { [pscustomobject]@{Hash='abc'} }
function Invoke-CimMethod {[pscustomobject]@{ReturnValue=0;Sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value}}
function Get-CimInstance {
 param($ClassName,$Filter)
 if('__MODE__' -eq 'upgraded'){return}
 if($Filter -eq 'ProcessId=11'){return}
 if($script:stopped){return}
 if($Filter -eq 'ProcessId=22' -and '__MODE__' -eq 'changed'){ $copy=$server.PSObject.Copy();$copy.CreationDate=[datetime]'2026-01-02';return $copy }
 return $server
}
function Get-NetTCPConnection {
 [pscustomobject]@{LocalPort=9210;LocalAddress='127.0.0.1';OwningProcess=$(if('__MODE__' -eq 'foreign'){99}else{22})}
}
function Invoke-RestMethod { $script:stopped=$true }
try {Stop-OrphanedManagedServer; if('__MODE__' -eq 'upgraded'){if($script:stopped){exit 2}} elseif('__MODE__' -ne 'valid' -or -not $script:stopped){exit 2}}
catch {if('__MODE__' -in @('valid','upgraded') -or $script:stopped){Write-Output $_;exit 3}}
exit 0
""".replace('__SOURCE__', str(source)).replace('__MODE__', mode)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_exact_orphan_is_shutdown(self): self.check('valid')
    def test_foreign_port_owner_is_preserved(self): self.check('foreign')
    def test_live_browser_is_preserved(self): self.check('live')
    def test_reused_pid_is_preserved(self): self.check('changed')

    def test_previous_version_record_without_server_does_not_block_upgrade(self): self.check('upgraded')


@unittest.skipUnless(sys.platform == "win32", "Windows launcher")
class ProfileIdentityTests(unittest.TestCase):
    def test_actual_launcher_functions_recognize_restart_and_reject_other_profiles(self):
        root = Path(__file__).resolve().parents[1]
        code = r'''
. (Join-Path '__ROOT__' 'neo-runtime-common.ps1')
function Test-NeoEndpointIdentity {param($Endpoint) return $true} # Explicit transport-only fixture; identity has separate tests.
$plan=[pscustomobject]@{profile_root='D:/owned/profile'}
$exe='D:\owned\chrome.exe';$browserExe=$exe;$profile='D:\owned\profile'
function Get-CimInstance {param($ClassName,$Filter) $script:fixture}
$cases=@(
 @{args="--user-data-dir=$profile --no-first-run";match=$true},
 @{args='--restart --user-data-dir="'+$profile+'"';match=$true},
 @{args="--user-data-dir=$profile-other";match=$false},
 @{args='--user-data-dir="'+$profile+'-other"';match=$false},
 @{args="--user-data-dir=$profile --type=renderer";match=$false},
 @{args="--log-file=$profile";match=$false}
)
foreach($spec in @(@{file='start-neo.ps1';name='Get-DedicatedBrowser'},@{file='ensure-neo-mcp.ps1';name='Get-Browser'})){
 $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path '__ROOT__' $spec.file),[ref]$null,[ref]$null)
 $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $spec.name},$true)
 Invoke-Expression $definition.Extent.Text
 foreach($case in $cases){
  $script:fixture=[pscustomobject]@{ExecutablePath=$exe;CommandLine='"'+$exe+'" '+$case.args}
  $matches=@(& $spec.name).Count
  if(($matches -eq 1) -ne $case.match){Write-Output $spec.file;exit 1}
 }
}
exit 0
'''.replace('__ROOT__', str(root))
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(sys.platform == 'win32', 'Windows launcher')
class SharedRuntimeHelperTests(unittest.TestCase):
    def run_ps(self,code):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',("$plan=[pscustomobject]@{python_executable='"+sys.executable.replace("'","''")+"'};\n"+code).replace('__ROOT__',str(root)).replace('__PYTHON__',sys.executable.replace("'","''"))],
                              capture_output=True,timeout=15)
        diagnostic=(result.stdout+result.stderr).decode('utf-8',errors='replace')
        self.assertEqual(result.returncode,0,diagnostic)

    def test_import_has_no_runtime_effects_and_readiness_cleans_its_session(self):
        self.run_ps(r'''$ErrorActionPreference='Stop'
function Get-CimInstance {throw 'unexpected process inspection'}
function Start-Process {throw 'unexpected start'}
function Add-Type {throw 'unexpected native initialization'}
function New-Item {throw 'unexpected write'}
. (Join-Path '__ROOT__' 'neo-runtime-common.ps1')
function Test-NeoEndpointIdentity {param($Endpoint) return $true} # Explicit transport-only fixture; identity has separate tests.
$plan=[pscustomobject]@{mcp_endpoint='http://127.0.0.1:9010/mcp';python_executable='__PYTHON__'}
foreach($case in @('good','missing-run','request-error')) {
 $script:requests=@();$script:case=$case
 function Invoke-WebRequest {
  param([switch]$UseBasicParsing,$Uri,$Method,$Headers,$ContentType,$Body,$TimeoutSec)
  $script:requests+=@{method=$Method;body=$Body}
  if($Method -eq 'Delete'){return}
  if($Body -like '*"initialize"*'){return [pscustomobject]@{Headers=@{'Mcp-Session-Id'='temporary-readiness'};Content='{"id":1,"result":{"protocolVersion":"2025-06-18"}}'}}
  if($Body -like '*notifications/initialized*'){return}
  if($script:case -eq 'request-error'){throw 'fixture request failure'}
  $name=if($script:case -eq 'missing-run'){'another-tool'}else{'run'}
  [pscustomobject]@{Content=('data: {"id":2,"result":{"tools":[{"name":"'+$name+'"}]}}')}
 }
 $ready=Test-McpReady
 if($ready -ne ($case -eq 'good')){throw "Unexpected readiness: $case"}
 if(@($script:requests | Where-Object {$_.method -eq 'Delete'}).Count -ne 1){throw 'Readiness session leaked'}
}
$reply=[pscustomobject]@{Content=[Text.Encoding]::UTF8.GetBytes('data: {"id":1,"result":{}}'+"`n"+'data: {"id":2,"result":{"kept":true}}')}
if(-not (Read-Rpc $reply 2).result.kept){throw 'SSE matching result lost'}
''')

    def test_atomic_writer_preserves_json_and_propagates_failure(self):
        import json,os,tempfile
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as folder:
            target=Path(folder)/'runtime.json'
            blocked=Path(folder)/'directory';blocked.mkdir()
            self.run_ps(r'''. (Join-Path '__ROOT__' 'neo-runtime-common.ps1')
function Test-NeoEndpointIdentity {param($Endpoint) return $true} # Explicit transport-only fixture; identity has separate tests.
Write-JsonAtomic '__TARGET__' @{process=@{pid=123;command='synthetic-only'};retained=$true}
try {Write-JsonAtomic '__BLOCKED__' @{};throw 'Failure was swallowed'}
catch {if($_.Exception.Message -ne 'Atomic runtime state write failed'){throw}}
exit 0
'''.replace('__TARGET__',str(target)).replace('__BLOCKED__',str(blocked)))
            self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['process']['pid'],123)

    def test_publisher_and_signature_must_both_match(self):
        self.run_ps(r'''. (Join-Path '__ROOT__' 'neo-runtime-common.ps1')
function Test-NeoEndpointIdentity {param($Endpoint) return $true} # Explicit transport-only fixture; identity has separate tests.
$cert=[pscustomobject]@{publisher='Approved Neo Publisher'}
$cert | Add-Member -MemberType ScriptMethod -Name GetNameInfo -Value {param($kind,$issuer) $this.publisher}
foreach($case in @(@{status='Valid';publisher='Approved Neo Publisher';allow=$true},@{status='UnknownError';publisher='Approved Neo Publisher';allow=$false},@{status='Valid';publisher='Another Publisher';allow=$false})) {
 $signature=[pscustomobject]@{Status=$case.status;SignerCertificate=$cert}
 if((Test-NeoPublisherSignature $signature $case.publisher) -ne $case.allow){throw 'Signature/publisher boundary changed'}
}
''')
