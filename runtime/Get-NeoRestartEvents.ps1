[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PlanPath,
    [Parameter(Mandatory)][string]$IncidentTime,
    [Parameter(Mandatory)][uint32[]]$BrowserPid,
    [ValidateRange(1,60)][int]$MinutesBefore=25,
    [ValidateRange(1,60)][int]$MinutesAfter=3
)
$ErrorActionPreference='Stop'

function Convert-AuditPid {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    if ($Text.StartsWith('0x',[StringComparison]::OrdinalIgnoreCase)) {
        return [Convert]::ToUInt32($Text.Substring(2),16)
    }
    return [Convert]::ToUInt32($Text,10)
}

function Convert-NeoAuditEvent {
    param([xml]$Xml,[string]$ExpectedExecutable,[uint32[]]$ExpectedPids)
    $kind=[int]$Xml.Event.System.EventID
    if ($kind -notin @(4688,4689)) { return }
    $fields=@{}
    foreach ($node in $Xml.SelectNodes("//*[local-name()='EventData']/*[local-name()='Data']")) {
        $fields[$node.GetAttribute('Name')]=$node.InnerText
    }
    $created=$kind -eq 4688
    $processPath=if ($created) { $fields.NewProcessName } else { $fields.ProcessName }
    if ($processPath -ine $ExpectedExecutable) { return }
    $processId=if ($created) { Convert-AuditPid $fields.NewProcessId } else { Convert-AuditPid $fields.ProcessId }
    if ($null -eq $processId -or $processId -notin $ExpectedPids) { return }
    $time=[DateTimeOffset]::Parse([string]$Xml.Event.System.TimeCreated.SystemTime)
    $row=[ordered]@{
        timeUtc=$time.UtcDateTime.ToString('o')
        eventId=$kind
        eventRecordId=[long]$Xml.Event.System.EventRecordID
        browserPid=$processId
    }
    if ($created) {
        $row.parentPid=Convert-AuditPid $fields.ProcessId
        $row.parentExecutable=if ($fields.ParentProcessName) { [IO.Path]::GetFileName($fields.ParentProcessName) }
        $row.commandLineRecorded=-not [string]::IsNullOrWhiteSpace($fields.CommandLine)
        if ($row.commandLineRecorded) {
            $row.restartSwitchMatchMode='command_line_text'
            $row.restartSwitch=[bool]($fields.CommandLine -match '(?i)(?:^|[\s"])--restart(?:$|[\s"=])')
            $row.relaunchedSwitch=[bool]($fields.CommandLine -match '(?i)(?:^|[\s"])--relaunched(?:$|[\s"=])')
            $row.waitForParentHandleSwitch=[bool]($fields.CommandLine -match '(?i)(?:^|[\s"])--wait-for-parent-handle(?:$|[\s"=])')
        }
    } else {
        $row.exitStatus=$fields.Status
    }
    [pscustomobject]$row
}

if ($IncidentTime -notmatch '(?:Z|[+-]\d{2}:\d{2})$') {
    throw 'Use an incident time with its UTC offset, for example 2025-02-03T10:53:02+09:00.'
}
$when=[DateTimeOffset]::Parse($IncidentTime,[Globalization.CultureInfo]::InvariantCulture)
$plan=Get-Content -Encoding UTF8 -LiteralPath $PlanPath -Raw | ConvertFrom-Json
if ($plan.runtime_mode -ne 'DIRECT_LOOPBACK') { throw 'Select the plan of the affected direct Neo installation.' }
$browser=[IO.Path]::GetFullPath((Join-Path $plan.proposed_runtime_root 'Chrome-bin\chrome.exe'))
$start=$when.AddMinutes(-$MinutesBefore).UtcDateTime.ToString('o')
$end=$when.AddMinutes($MinutesAfter).UtcDateTime.ToString('o')
# Match the UTC timestamp in the event itself. Windows PowerShell's
# FilterHashtable time conversion can shift the window by the local offset.
$query="*[System[(EventID=4688 or EventID=4689) and TimeCreated[@SystemTime >= '$start' and @SystemTime <= '$end']]]"
$status='ok'
$rows=@()
$errorId=$null
try {
    $rows=@(Get-WinEvent -LogName Security -FilterXPath $query |
        ForEach-Object { Convert-NeoAuditEvent ([xml]$_.ToXml()) $browser $BrowserPid } |
        Sort-Object eventRecordId)
    if ($rows.Count -eq 0) { $status='no_matching_neo_events' }
} catch {
    $errorId=$_.FullyQualifiedErrorId
    if ($errorId -like 'NoMatchingEventsFound,*') { $status='no_audit_events_in_window' }
    else { $status='audit_read_failed' }
}
[pscustomobject]@{
    queryStatus=$status
    errorId=$errorId
    mcpPort=([Uri]$plan.mcp_endpoint).Port
    cdpPort=$plan.cdp_port
    startUtc=$start
    endUtc=$end
    expectedBrowserPids=$BrowserPid
    events=$rows
} | ConvertTo-Json -Depth 5
