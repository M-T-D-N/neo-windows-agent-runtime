# Neo Windows Agent Runtime (unofficial)

Portable Windows launcher, MCP stdio bridge, task tab recovery and cleanup for
BrowserOS neo. This is a candidate built from the currently qualified **Neo 0.51.0 /
server 0.0.65**, not a claim to support the latest upstream version. DOT review is
required before the first public upload. Current-PC isolated tests and other-PC
tests are reported separately; the latter are deferred by the owner.

## v0.1.2: registered popup tabs after parent closure

Closing the last owned parent no longer strands its binding when a link-opened,
unclaimed child remains in the same group. Native absence, the saved full group
tab IDs, exact registered target/tab/URL and every current alias must agree.
Known pre-existing unowned siblings cannot fall back to URL restoration when
registration fails, even when the parent and child have the same URL.
Unknown group members, other owners, changed registrations or a surviving parent
preserve the binding. The child is never adopted or closed by reconciliation;
its contents and used-tab cleanup scope remain intact for explicit review.

Browser 0.51.0, patched server 0.0.65, installer/dependency pins and corresponding
server source remain unchanged. This release adds no ownership permissions.

After an explicitly authorized administrator close-all, cached clients can
consume a complete closed reset receipt without starting Neo. A fresh
browser_reset_id UUID, immutable browser_reset_verified=true,
browser_reset_pending=true, browser_closed=true, null
session/pages_session, and empty pages/groups/used_tabs/retained/prior_bindings/
review_required arrays are required. Output inventories remain intact. A new
server handle consumes the pending receipt; readiness failures cannot revive
old hints. Other cached clients can read the pending or consumed state and reuse
the current handle. Begin only acknowledges retirement. Each connection must
receive an explicit successful tabs action=list, select current page IDs/session,
and echo its inventory_receipt on fresh browser and page-using cleanup requests.
The ephemeral receipt is returned in text and com.workspace.neo/inventory_receipt
metadata; it is never persisted or forwarded upstream. Internal inventory,
windows/groups lists and failed lists do not authorize page operations. Old queued
requests and retired explicit session hints are rejected rather than replayed
against reused page numbers. Local begin/outputs/check need no receipt.
Normally close with native browser tools first. Then hold
state.browser_operation(exclusive=True) from snapshot capture through exact
browser/server termination verification and all administrator writes:
TaskState._write_record(administrator_reset=True, operation_guard=guard,
expected_state=<deep copy read under the guard before closure verification>).
Browser tools must not be invoked while administration holds this guard.
The profile-wide OS lock permits concurrent shared Codex/AGY Bridge.handle calls;
exclusive administration waits for readiness, internal reconciliation, in-flight
HTTP and result bookkeeping, while blocking new calls. Raw/stateless clients
outside the adapter are not covered and must not be active at administration.
After an epoch change an old queued request stops before dispatch until local
workspace_cleanup begin acknowledges retirement and fresh inventory is inspected.
The same transaction rejects changed state and reused reset
UUIDs. Every task write advances a revision; mutation-capable dispatch persists
it before action, so a later operation cannot return silently to an equal-looking
old snapshot. Transactions preserve the union of cached/on-disk
output receipts and reject provenance conflicts or stale reset writes. Two
in-flight reset mints cannot replace the first consumed handle; the later caller
refreshes current state and stops its stale preflight before further actions.
This mechanism never closes tabs or grants ownership by itself. Raw parent
targets missing from the SDK listing also block URL recovery.

## v0.1.1: closed-group reconciliation fix

Closing the last tab in one task group no longer leaves an unrecoverable stale
binding when a different group shares its session title. The bridge checks the
complete native target list and exact group identity before recognizing absence.
SDK-only aliases are excluded only after a per-target CDP query returns the
explicit no-such-target error. Live targets, other errors and incomplete proof
preserve the prior binding. Arbitrary omissions from both catalogs cannot be
independently detected; normal CDP catalog completeness remains a trust premise.
A surviving native target with a missing SDK alias, ownership conflicts and
ambiguous restoration still preserve the prior binding. Reconnect the MCP client
after changing the installed source; reuse the same task UUID and existing tabs.

This is a Python bridge/state fix. Browser 0.51.0, patched server 0.0.65, binary
pins, corresponding server source, installer, endpoint checks and dependencies
are unchanged from v0.1.0. Install this source into a fresh root as documented;
the existing installer intentionally refuses to overwrite a different source ID.

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
and verify the public result before marking success. Keep one publishing PC per
operation or coordinate state explicitly.

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

For a whole-browser restart that must preserve task bindings, quit the browser
application normally (Exit / supported `Browser.close`) after inspecting pending
and unsaved work. Programmatic `windows close` is a window-removal action: its
success can clear the task's closed-tab record, even when Chromium later restores
the old window as user tabs. Do not use window removal as a restart shortcut.

`runtime/update-runtime.py` checks the official signed feed and can prepare a
candidate in `local/scratch/<owned-task>`. It does not promote a new version.
Keep browser/server/catalog compatibility and task preservation verified before
switching versions. The bundled launcher disables the upstream installer updater;
no weekly automation is installed for you.

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
