# Neo Windows Agent Runtime (unofficial)

Portable Windows launcher, MCP stdio bridge, task tab recovery and cleanup for
BrowserOS neo. The qualified combination is **BrowserOS neo 0.52.1.1**
(Chromium 155.0.8318.26) and the protected **Claw Server 0.0.65**.
Current-PC isolated qualification covers this version pair. Other-PC testing is deferred.

## v0.1.9: browser update and screen-lock restart

The launcher disables Chromium's screen-lock Smart Restart trigger for the dedicated
browser. The managed installer verification and browser update flow are retained.
The release uses the same qualified server binary and corresponding source as the
preceding release, preserving its tab ownership and durable shutdown behavior.

After a browser replacement, list current tabs through the active MCP connection
before continuing. The bridge preserves the server and profile identity checks and
requires a fresh tab inventory before a request is sent to the replacement browser.
The option is applied when the dedicated launcher creates a new browser process.
`start-neo.ps1 -Background` reuses an existing browser and leaves its startup
options in effect. At a safe restart boundary, verify that unsaved input and
active requests are clear. Use the normal shutdown command below, start it again,
and verify that the new browser process has `--disable-features=SmartRestartLockScreen`.

Tab restoration depends on Chromium's saved session state. Verify the required
windows and tabs after restart. A normal restart can leave a previously visible
tab absent, especially a closed tab or a live remote session. This runtime does
not reconnect remote sessions automatically. Save in-progress work beforehand.
The verification used a local representative remote-session page, without a live
Chrome Remote Desktop connection.

## v0.1.6: one physical tab and background restoration

Tab creation reuses a handle registered by a concurrent inventory. Existing
physical aliases keep their IDs and ownership while URL, title, loading, group
and window metadata are synchronized. No group is adopted based on its URL.

The server includes the same protected expired/restored recovery patch chain as
the qualified Workspace runtime. Recovery still requires the original evidence,
complete current inventory and native ownership; it never recreates business tabs
or treats matching URLs as ownership.

After a backend-only restart, `workspace_cleanup resume` can reclaim the original
physical tabs without manual selection when the previous claim, exact group
membership and current owner checks agree. Close disposable tabs individually
after checking for pending or unsaved work. A different physical tab still needs
the explicit restored-group procedure and evidence of a normal shutdown.

To stop the dedicated browser when it is safe to interrupt, run
`.venv\Scripts\python.exe -B -X utf8 runtime\stop-neo.py --confirmed-safe`
from the installation root. Verify there is no unsaved or in-progress work
before running it. Shutdown ends open MCP event streams, verifies the durable
session and claim records, then requests a normal browser quit without focusing
Neo. `runtime\start-neo.ps1 -Background` starts it in the background again.

Chromium can dismiss a beforeunload guard while a native window is minimized.
Before explicit input (including fill), navigation/history/reload or tab closure,
this build checks the target window. A minimized or hidden Windows window is
restored without activation, BEHIND the current foreground app. It derives the
browser PID from the same CDP connection, requires one existing normal browser
window and one ownerless native Chrome window for that PID, preserves an open
process handle, and verifies foreground, Z-order, visibility and the same CDP
window after restoration. Unknown/multiple windows, an unavailable foreground,
a topmost window or failed validation prevent the original action. No new window,
window activation, SetForegroundWindow or global setting change is used.

DOM reads do not restore windows. Dialog decisions remain explicit; saved-content
readback is still required. Concurrent user changes after the check and arbitrary
navigation scripts through evaluate/raw CDP are outside this guard. Inspect their
outcomes and never retry an uncertain save or submission. A system-tray mode that
replaces the native window is not claimed equivalent to hiding the same HWND.

Guarded operations carry the exact target, tab, connection epoch and original
CDP session through input/ref/frame resolution, navigation and target-session
`Page.close`. A rebind during preparation or a snapshot from another root session
is rejected before input. Prepared input never retries on a newly resolved target.

Blanket `Browser.closeWindow` is disabled. `windows close` is a nonexecuting
per-tab cleanup plan with `requestedActionExecuted=false`; it never closes even
internal new-tab anchors and grants no close rights. Close only individually
authorized physical tabs after unsaved/pending checks. If the last permitted tab
ends the browser, verify the exact process identity has exited using the runtime's
read-only check. CDP loss alone is insufficient, and checking must not relaunch
Neo. Unknown/user tabs remain unless their closure is explicitly authorized.

Tab closure validates every target identity and atomically compares its original
incarnation before removing a binding. A delayed move reply cannot recreate a
closed handle or overwrite a changed binding.

## v0.1.5: reconnect initialization shared by Codex and AGY

Binding the actual task UUID performs local initialization without a redundant
`begin`. After a recorded reset, a premature page/cleanup action returns only a
fresh caller-visible tab catalog and current session/receipt. The explicit
`NEO_RECONNECT_INITIALIZED`, `requestedActionExecuted=false` and `isError=true`
response means the submitted operation was not executed. Select current IDs and
construct a new request. No stale or queued page operation is replayed.

This initialization path never starts an off browser, adopts groups or infers an
unknown task. Failed inventory/delivery, retired sessions and wrong receipts
remain blocked. The export also synchronizes the current operating recovery
guards and their tests; the complete delta is reviewed before publication.
Read-only cleanup preserves original restoration evidence across renamed, empty
or incomplete inventories and does not mark unresolved restoration complete.
The final save boundary also requires safe native ownership or closure evidence.
Equal URLs in another group never discharge a missing prior group. Each selected
recovery replaces only its proven prior group, with ownership checked across all
aliases of each target. Legacy adoption requires a completed native operation;
read-only cleanup cannot manufacture an adoption receipt.
Browser/server versions, dependency pins, profiles and permissions are unchanged.

## Browser replacement and MCP diagnostics

The launcher’s optional MCP readiness probe reports the listener and browser identity
separately. Inspect `mcpIdentity.code` and `mcpReadinessError` in the result:

| Code | Observed state |
| --- | --- |
| `MCP_NOT_LISTENING` | No listener on the configured MCP port. |
| `CDP_NOT_LISTENING` | The recorded browser exists; its CDP listener is unavailable. |
| `RECORDED_BROWSER_EXITED` | The owned MCP server is listening and CDP has no listener. |
| `BROWSER_RECORD_STALE` | The owned MCP server is listening, the recorded browser exited, and a new loopback CDP owner needs verification. |
| `IDENTITY_UNVERIFIED` | An exact process, profile, configuration, or socket check failed. |

An actual browser request can repair a stale browser record through the
installation mutex. This path verifies the existing processes and only updates
the recorded browser identity. It sends no browser action, opens no window, and
leaves the existing server running. After recovery, request a fresh tab list
and use the current page IDs. The bridge rejects queued requests from before
the replacement. Local tool discovery and the passive event stream never
perform the repair.

If a transport reset occurs while an owned listener is still present, the
bridge retries MCP readiness briefly. The same rule covers the gap between
the old browser exiting and its replacement acquiring CDP. That request does
not invoke the launcher if the existing listener disappears during the wait.

When Windows process auditing was enabled at the time of the incident, run
the following in an elevated Windows PowerShell from the installation root.
Use the affected installation's plan and replace the timestamp and PIDs.

```powershell
& .\runtime\Get-NeoRestartEvents.ps1 -PlanPath .\runtime\runtime-plan.json `
    -IncidentTime '2025-02-03T10:53:02+09:00' -BrowserPid 7001,7002
```

The query compares UTC timestamps directly in the Windows Security log. It
reports the process creator and exit code. When Windows recorded the command
line, the helper searches its text for restart switches. These hints can also
match text inside quoted arguments. The creator PID and exit code alone do
not identify the origin of a restart request.

Use the timestamped Chromium and recovery-server logs plus the recorded process
IDs and creation times when investigating an unexpected relaunch. A CDP
disconnect or reconnect by itself identifies a change in the connection only.

## v0.1.4: identity records after a browser-only restart

Equivalent absolute config paths use normalized comparison; relative config
arguments, other files and additional arguments remain rejected. The launcher
recognizes both `--user-data-dir="path with spaces"` and
`"--user-data-dir=path with spaces"`, so it reuses the same dedicated browser.
Exactly one profile switch is required, including when duplicate values are
identical; conflicting or repeated profile switches never qualify a bootstrap.
Process command lines use Windows `CommandLineToArgvW` via in-memory P/Invoke
(no compiler files). Profile-switch aliases using `-`, `/` or different casing
are counted together with `--`; only one canonical `--user-data-dir=value`
qualifies. The endpoint guard, rebind and managed fallback share this rule.
Automatic ownership accepts the dedicated launcher's unambiguous argument form:
argv[0] is the program, profile paths must be absolute, and edge whitespace or
control/format characters, empty arguments, `--` and `--single-argument` modes
are left unqualified. An unqualified root of this installation blocks startup
and shutdown instead of being treated as browser absence. A live recorded
browser PID or any occupied CDP endpoint also preserves its recovery server.

When the recorded browser has ended but its exact server survives, an explicit
launcher invocation can refresh only the browser PID/time record. It requires a
unique replacement using the dedicated executable/profile, the same Windows user
and desktop session, exclusive ownership of the configured loopback CDP port,
the exact recorded server/config/binary/listener and an unchanged final snapshot.
A live or reused old PID, competing profile or unknown owner preserves the record.
Read-only endpoint checks never repair state or start a browser. Normal server
startup still uses its existing bounded readiness wait. No website action is
replayed. Browser/server/dependency pins are unchanged.

This is qualified on an isolated installation on the maintainer's current Windows
PC, including a Unicode path with spaces; other-PC testing remains deferred.

## Requirements and installation

- Windows x64, Python **3.13 x64**, Windows PowerShell 5.1, `tar` with 7z support.
- Microsoft Visual C++ **v14 x64 Redistributable** (`VCRUNTIME140.dll` in System32),
  from the [official Microsoft download page](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).
  The installer checks for it and does not change a shared system runtime.
- A new absolute installation directory you own, outside this checkout. No admin
  installation, registry edits, default browser changes or shared Python changes.
- The qualified patched server asset, whose SHA-256 is in `pins.json`. The public
  release must provide the matching complete source, license, patches and build
  instructions alongside this binary. Browser binaries are downloaded only from
  the pinned official CDN and are verified by size, SHA-256 and Authenticode.
- For source rebuilds: Git, Rust 1.98 and MSVC build tools; see `build-server.ps1`.
  Build outputs may have different hashes. They need their own qualification and
  catalog before use; do not bypass the binary pin.

```powershell
py -3.13 install.py --root E:\AgentTools\Neo --server E:\Downloads\browseros-claw-server.exe
```

The installer creates a private `.venv`, signed browser payload, pinned server,
`runtime/runtime-plan.json`, and local client templates. It **does not launch Neo**.
It refuses existing nonempty or incomplete installations. Running the same command
again verifies the existing installation without resetting its profile. Keep a
failed installation for diagnosis; choose a fresh root for a new attempt.

For offline dependencies add `--wheelhouse E:\Downloads\NeoWheels`. For a cached
official installer add `--installer E:\Downloads\BrowserOS_neo_installer.exe`.
To test alongside another installation choose all three unused ports explicitly:
`--proxy-port 19010 --cdp-port 19111 --backend-port 19210`. A single profile must
never be shared by two active installations.

## Connect and use

Merge the generated `local/templates/codex.toml` MCP section into your own Codex
configuration after checking for an existing server of the same name. The AGY JSON
fragment is generated separately; merge it using that client's supported MCP
configuration. This installer does not overwrite either client's settings or grant
blanket tool permissions. Neither client requires a Neo browser extension.

For AGY CLI tasks, `runtime/run-agy-neo.py --task-id <actual-stable-task-uuid> --
<your-supported-agy-arguments>` wraps the existing CLI and checks cleanup at a
successful finish boundary. Resume the same AGY conversation and UUID. A cleanup
failure (exit 6) is not publication success. Native AGY UI/permission configuration
is a separate host integration; a passing stdio test alone does not verify it.

Install `local/templates/SKILL.md` in the client's supported skill location, then
set its installation path to your chosen root. Read the Stop-hook fragment before
merging it: `mcp_tool`, `${session_id}` and `${stop_hook_active}` require host support.
This hook fragment is **unverified on stock Codex/AGY hosts**, including conversion
of `stop_hook_active` to the boolean required by the cleanup schema.
If the client does not support those hooks, run the same cleanup check explicitly
before ending browser work. Do not advertise unsupported host features as active.

`initialize` and `tools/list` are local and must not open a browser. A real browser
request may start Neo. You can also launch it explicitly:

```powershell
& E:\AgentTools\Neo\runtime\start-neo.ps1
& E:\AgentTools\Neo\runtime\start-neo.ps1 -Check
```

Log in locally on each PC. **Never copy cookies, auth tokens, profiles, task UUID
ledgers, or client secrets into the public repository or between active PCs.**
There is no scheduled/startup launcher installed by this package.

For each task, bind its actual stable UUID with `workspace_cleanup begin`, then
`resume` recorded task groups before opening a tab. Inspect existing windows and
tabs, use the relevant one, and register actual business-page reuse with `use`.
Read-only inspection grants no ownership or close rights. Close completed disposable
owned tabs, verify their absence and run `check`. Preserve drafts, pending work,
user tabs and other active owners. Retain only a concrete next use; do not silently
create duplicate tabs when restoration is ambiguous.

Do not automatically accept `beforeunload`; preserve unsaved content before
retrying navigation or closing. Never replay an input, upload or publish call whose
result was lost. The local publishing-intent ledger can block a repeated local
operation; **it is not distributed across PCs and is not an automatic wrapper
around every site's Publish button**. A publishing workflow must use it explicitly
and verify the public result before marking success. Start with
`python -B -m web_automation register`, using the same approved manuscript version,
target and ordered `--attachment-file` inputs across clients. Keep the returned
ID for `begin` and `reconcile`. Submit only when `begin` succeeds with
`SUBMITTING`. A `VERIFIED` result carries the existing post. An authorized
repeat needs `--repeat` and a new approval reference. A plausible older entry
with opaque attachment identifiers blocks registration. After comparing the
original ordered file bytes, use `--verified-legacy-operation <id>` to retrieve
that entry and its saved outcome. Keep one
publishing PC per operation or coordinate state explicitly.

## Local editor and publishing-intent check

The optional fictional fixture exercises rich text, iframe and plain editors,
real image-byte uploads, saved-draft reopening and a durable submit guard. It
does not sign into or publish to any external site. Use a fresh owned run path:

```powershell
Set-Location E:\AgentTools\Neo\runtime
& ..\.venv\Scripts\python.exe -B -m web_automation serve --state-root 'E:\AgentTools\Neo\local\scratch\editor-trial'
```

Open the printed loopback URL through Neo MCP. Finish and close only your test
tabs, then stop this foreground fixture with Ctrl+C. Its synthetic data is under
the selected run path. `python -m web_automation --help` also documents the local
intent-ledger commands; integrate them explicitly into a publishing workflow.

## Reconnect, updates and removal

If the host exposes `mcp_control.restart`, use it after in-flight calls end, then
perform a real read-only Neo query. This is a **Codex host capability**, not a tool
supplied by the Neo server; unmodified hosts may need their own supported MCP
restart UI. AGY also uses its own supported reconnect path. A new MCP connection
must restore task identity before opening replacement tabs.

The shared bridge initializes locally when the actual task UUID is bound; no
second `begin` is needed after reconnect. After a recorded reset, a premature
page operation returns `NEO_RECONNECT_INITIALIZED` with a fresh tab catalog only.
`requestedActionExecuted=false` and `isError=true` mean the original operation
did not run. Select current IDs/session and construct a new request with the
returned `inventory_receipt`. Initialization never starts an off browser, adopts
groups, or replays a save/upload/post. Failed inventory/delivery and previously
queued requests cannot authorize page operations.

For a managed restart that preserves task tabs, check active requests and unsaved
input, then run the installed runtime's `stop-neo.py --confirmed-safe` with its
private Python. The command verifies the dedicated process and loopback identity,
waits for a successful server exit and durable session ends and claim releases,
then quits that same browser. An unresolved drain or unknown session end blocks
closure. Use the installed `start-neo.ps1 -Background` for a nonactivating
automation restart. The interactive shortcut uses a normal foreground launch.

For a whole-browser restart that must preserve task bindings, quit the browser
application normally (Exit / supported `Browser.close`) after inspecting pending
and unsaved work. `windows close` only returns a nonexecuting cleanup plan. It is neither a normal
application quit nor a restart shortcut. Close only authorized individual tabs
when their work is complete; use an explicitly authorized normal application quit
when preserving active tabs for a whole-browser restart.

`runtime/update-runtime.py` checks the official signed feed and can prepare a
candidate in `local/scratch/<owned-task>`. It does not promote a new version.
Keep browser/server/catalog compatibility and task preservation verified before
switching versions. The bundled launcher disables the upstream installer updater;
no weekly automation is installed for you.

`runtime/diagnose-neo.ps1` reports local process identity, configured listeners,
and the Windows registered browser version without opening an MCP session.
`-ProbeMcp` opts into a temporary readiness session and reports cleanup errors.
Use `-CheckUpdate` for the feed comparison.
Optional `-IncidentTime` and `-BrowserPid` select existing Windows process audit
events. Run `smoke-runtime.py --runtime <isolated-runtime> --client codex|agy`
from the distribution bundle for a fresh fixture with separate ports and profile.

To remove: finish active requests, preserve unsaved work, close only this dedicated
browser normally, disconnect only this MCP entry, and verify no exact process is
using this installation. Remove the generated client fragments and shortcuts you
installed. Recycle the installation directory with Explorer; `local/profile` and
`local/data` are user data, so preserve them if needed. There is no silent force
kill, uninstall script that deletes profiles, or global Python cleanup.

## Source, license and limits

AGPL-3.0-or-later; see `LICENSE` and `NOTICE`. Upstream commit and patch hashes are
in `pins.json`; upstream license: https://github.com/browseros-ai/BrowserOS/blob/main/LICENSE.
Third-party Python packages retain their licenses. Official browser payload keeps
its embedded licenses. This package is not endorsed by BrowserOS, OpenAI or AGY.

Maintainers: after intentional source edits, review the diff and run
`python -B refresh-manifest.py --write`, then rerun installation and relevant tests.
The command updates only existing manifest members. Add new source members through
the export allowlist; never hash a whole developer checkout or its local state.

No authentication is added to the local MCP/CDP services. Do not expose these ports
to a network or port-forward them. This adapter disables the upstream managed
server/proxy with its official `--disable-browseros-server` switch, retains CDP,
and uses the existing exact-identity loopback direct-server path. Verify actual
listener addresses before use; do not assume a URL containing `127.0.0.1` proves
loopback-only binding. Broader listeners are a release blocker.

CPU-only browser/HTTP/file I/O; GPU/JIT/TensorRT acceleration is not applicable.
Release notes must distinguish mock regression tests, isolated real-browser tests,
client-host tests and cross-PC tests. A test client is not proof that another
client's currently loaded MCP connection works.

The `CleanupPlan:` JSON is included in MCP text. This tool has no output schema,
so wire conversion omits `structuredContent`; do not depend on that field. The
plan grants no closure rights and executes no window/tab close.
