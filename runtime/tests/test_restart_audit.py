import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell event query')
class RestartAuditTests(unittest.TestCase):
    def test_offsets_select_the_same_utc_window_and_preserve_event_identity(self):
        script = Path(__file__).resolve().parents[1] / 'Get-NeoRestartEvents.ps1'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as scratch:
            plan = Path(scratch) / 'runtime-plan.json'
            plan.write_text(json.dumps({
                'runtime_mode': 'DIRECT_LOOPBACK',
                'proposed_runtime_root': r'C:\synthetic\Neo\browser',
                'mcp_endpoint': 'http://127.0.0.1:19030/mcp',
                'cdp_port': 19131,
            }), encoding='utf-8')
            fixture = r"""
function Get-WinEvent {
    param([string]$LogName,[string]$FilterXPath)
    if ($LogName -ne 'Security' -or $FilterXPath -ne "*[System[(EventID=4688 or EventID=4689) and TimeCreated[@SystemTime >= '2025-02-03T01:28:02.0000000Z' and @SystemTime <= '2025-02-03T01:56:02.0000000Z']]]") {
        throw 'Incorrect audit time filter'
    }
    foreach ($id in 4689,4688) {
        $recordId=if($id -eq 4688){101}else{102}
        $row=[pscustomobject]@{Xml=@"
<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
<System><EventID>$id</EventID><EventRecordID>$recordId</EventRecordID><TimeCreated SystemTime="2025-02-03T01:53:02.4710000Z" /></System>
<EventData>
<Data Name="NewProcessName">C:\synthetic\Neo\browser\Chrome-bin\chrome.exe</Data>
<Data Name="NewProcessId">0x1b5a</Data><Data Name="ProcessId">0x1b59</Data>
<Data Name="ProcessName">C:\synthetic\Neo\browser\Chrome-bin\chrome.exe</Data>
<Data Name="ParentProcessName">C:\synthetic\Neo\browser\Chrome-bin\chrome.exe</Data>
<Data Name="CommandLine">chrome.exe --restart --wait-for-parent-handle=12345</Data>
<Data Name="Status">0x0</Data>
</EventData></Event>
"@}
        $row | Add-Member -MemberType ScriptMethod -Name ToXml -Value {$this.Xml} -PassThru
    }
}
& '__SCRIPT__' -PlanPath '__PLAN__' -IncidentTime '__INCIDENT__' -BrowserPid 7001,7002
"""
            fixture = fixture.replace('__SCRIPT__', str(script).replace("'", "''"))
            fixture = fixture.replace('__PLAN__', str(plan).replace("'", "''"))
            for incident in ('2025-02-03T10:53:02+09:00', '2025-02-03T01:53:02Z',
                             '2025-02-02T17:53:02-08:00'):
                with self.subTest(incident=incident):
                    completed = subprocess.run(
                        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                         '-Command', fixture.replace('__INCIDENT__', incident)],
                        capture_output=True, timeout=20,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors='replace'))
                    result = json.loads(completed.stdout)
                    self.assertEqual(result['queryStatus'], 'ok', result)
                    created, exited = result['events']
                    self.assertEqual((created['browserPid'], created['parentPid']), (7002, 7001))
                    self.assertEqual(created['restartSwitchMatchMode'], 'command_line_text')
                    self.assertTrue(created['restartSwitch'])
                    self.assertTrue(created['waitForParentHandleSwitch'])
                    self.assertEqual((exited['browserPid'], exited['exitStatus']), (7001, '0x0'))
