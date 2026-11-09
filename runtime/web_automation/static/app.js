'use strict';
const $ = id => document.getElementById(id);
let attachments = [], pending = 0, lastMode = 'rich', guard = null, guardLoading = true, guardRead = 0;
let uploadFailed = false, saveFailed = false, localUncertain = false, latchUnavailable = false;
const validOperation = () => /^[a-z0-9][a-z0-9_-]{0,79}$/.test($('operation').value);
function editor(mode = $('mode').value) { return mode === 'iframe' ? $('frame').contentDocument.getElementById('editor') : mode === 'plain' ? $('plain') : $('rich'); }
function html(mode = $('mode').value) { const e = editor(mode); if (!e) return ''; if (mode !== 'plain') return e.innerHTML; const div = document.createElement('div'); div.textContent = e.value; return div.innerHTML.replace(/\n/g, '<br>'); }
function plain() { const e = editor(); return e ? ($('mode').value === 'plain' ? e.value : e.innerText) : ''; }
function setBody(value) { if ($('mode').value === 'plain') { const div = document.createElement('div'); div.innerHTML = value; $('plain').value = div.innerText; } else editor().innerHTML = value; }
function report(message, value) { $('status').textContent = message; if (value !== undefined) $('result').textContent = JSON.stringify(value, null, 2); }
async function request(path, value) { const response = await fetch(path, value === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Fixture-Request': '1'}, body: JSON.stringify(value)}); const data = await response.json(); if (!response.ok) { const error = new Error(data.error || `HTTP ${response.status}`); error.operation = data.operation; throw error; } return data; }
function payload() { return {operation_id: $('operation').value, title: $('title').value, body_html: html(), plain_body: plain(), attachments: attachments.map(a => a.id), delay_ms: Number($('save-delay').value), fail: $('save-fail').checked}; }
function latchKey() { return `fixture-failures:${$('operation').value}`; }
function readLatches() { try { const saved = JSON.parse(sessionStorage.getItem(latchKey()) || '{}'); uploadFailed = Boolean(saved.uploadFailed); saveFailed = Boolean(saved.saveFailed); localUncertain = Boolean(saved.localUncertain); latchUnavailable = false; } catch (error) { latchUnavailable = true; } }
function persistLatches() { try { sessionStorage.setItem(latchKey(), JSON.stringify({uploadFailed, saveFailed, localUncertain})); } catch (error) { latchUnavailable = true; } }
function locked() { return guardLoading || latchUnavailable || localUncertain || uploadFailed || saveFailed || (guard && ['SUBMITTING','NEEDS_RECONCILIATION','VERIFIED','FAILED_CONFIRMED'].includes(guard.status)); }
function render() {
  // Replacing controls deliberately invalidates old DOM references.
  const invalid = !validOperation() || !$('title').value.trim() || (!plain().trim() && !html().includes('<img '));
  const save = document.createElement('button'); save.id = 'save'; save.textContent = 'Save draft'; save.disabled = pending > 0 || invalid; save.onclick = saveDraft;
  const reopen = document.createElement('button'); reopen.id = 'reopen'; reopen.textContent = 'Reopen saved draft'; reopen.disabled = pending > 0 || !validOperation(); reopen.onclick = reopenDraft;
  const publish = document.createElement('button'); publish.id = 'publish'; publish.textContent = 'Publish synthetic post'; publish.disabled = pending > 0 || invalid || locked(); publish.onclick = publishPost;
  $('operation').disabled = pending > 0; $('find').disabled = pending > 0 || !validOperation();
  $('actions').replaceChildren(save, reopen, publish);
}
async function loadGuard() {
  const read = ++guardRead, operationId = $('operation').value;
  guard = null; guardLoading = true; readLatches(); render();
  if (!validOperation()) { guardLoading = false; report('Operation ID must be a lowercase slug, at most 80 characters.'); render(); return; }
  try { const value = await request(`/api/guard/${encodeURIComponent(operationId)}`); if (read !== guardRead) return; guard = value.operation; guardLoading = false; if (guard && guard.status !== 'PREPARED') report(`Operation ${guard.status}; use Find posts for readback, never submit again.`, value); }
  catch (error) { if (read !== guardRead) return; guardLoading = true; report(`Guard state unavailable: ${error.message}; publish remains blocked.`); }
  render();
}
function renderAttachments() {
  $('attachments').replaceChildren();
  for (const item of attachments) { const li = document.createElement('li'); li.append(document.createTextNode(`${item.name} · ${item.size} bytes · SHA-256 ${item.sha256}`)); if (['image/png','image/jpeg','image/gif','image/webp'].includes(item.type)) { const image = document.createElement('img'); image.src = `/api/uploads/${item.id}`; image.alt = item.name; li.append(image); const insert = document.createElement('button'); insert.textContent = `Insert ${item.name}`; insert.disabled = pending > 0 || $('mode').value === 'plain'; insert.onclick = () => { editor().focus(); editor().ownerDocument.execCommand('insertImage', false, image.getAttribute('src')); render(); }; li.append(insert); } $('attachments').append(li); }
}
function mountFile() { const input = document.createElement('input'); input.type = 'file'; input.multiple = true; input.id = 'files'; input.setAttribute('aria-label', 'Upload attachments'); input.disabled = pending > 0; input.onchange = () => uploadFiles(Array.from(input.files)); $('file-control').replaceChildren(input); }
function fileBase64(file) { return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = () => reject(new Error('File read failed')); reader.readAsDataURL(file); }); }
async function uploadFiles(files) { pending++; render(); mountFile(); renderAttachments(); report('Uploading…'); try { for (const file of files) { if (attachments.length >= 16 || file.size > 4 * 1024 * 1024 || file.size === 0) throw new Error('Fixture upload limit: 16 files, each 1 byte to 4 MiB'); const value = await request('/api/upload', {name: file.name, type: file.type || 'application/octet-stream', base64: await fileBase64(file), delay_ms: Number($('upload-delay').value), fail: $('upload-fail').checked}); attachments.push(value); renderAttachments(); } if (files.length) uploadFailed = false; report('Uploads complete'); } catch (error) { uploadFailed = true; report(`Upload failed: ${error.message}. Correct the upload before publishing.`); } finally { pending--; persistLatches(); mountFile(); renderAttachments(); render(); } }
async function saveDraft() { pending++; render(); mountFile(); report('Saving draft…'); try { const value = await request('/api/draft', payload()); saveFailed = false; report('Draft saved', value); } catch (error) { saveFailed = true; report(`Draft save failed: ${error.message}. Save successfully before publishing.`); } finally { pending--; persistLatches(); render(); mountFile(); } }
async function reopenDraft() { pending++; render(); report('Reopening draft…'); try { const content = await request(`/api/drafts/${encodeURIComponent($('operation').value)}`); $('title').value = content.title; setBody(content.body_html); attachments = content.attachments; report('Saved draft reopened', content); } catch (error) { report(`Reopen failed: ${error.message}`); } finally { pending--; renderAttachments(); mountFile(); render(); } }
async function verifyOnePost() {
  const operationId = $('operation').value;
  const listed = await request(`/api/posts?operation_id=${encodeURIComponent(operationId)}`);
  $('posts').replaceChildren();
  for (const post of listed.posts) { const link = document.createElement('a'); link.href = post.url; link.textContent = `${post.post_id}: ${post.title}`; link.target = '_blank'; link.rel = 'noopener'; $('posts').append(link); }
  if (listed.posts.length !== 1) { report(`Found ${listed.posts.length} posts; exactly one saved post is required. Existing submit guard remains blocked.`, listed); return false; }
  // Explicit saved-content readback precedes server comparison with immutable intent.
  const post = await request(`/api/posts/${listed.posts[0].post_id}`);
  if (post.post_id !== listed.posts[0].post_id || post.operation_id !== operationId) throw new Error('Saved post ID or operation mismatch');
  const verified = await request('/api/guard/reconcile', {operation_id: operationId, post_id: post.post_id});
  guard = verified.operation; localUncertain = false; persistLatches();
  report(`Verified saved post ${post.post_id}; publish remains blocked for this operation.`, verified);
  return true;
}
async function publishPost() {
  if (pending || !validOperation() || locked()) { report('Publish guard blocks this operation.'); return; }
  pending++; render(); mountFile(); report('Persisting submit guard…');
  try {
    const value = payload(); const {delay_ms, fail, ...intent} = value;
    localUncertain = true; persistLatches();
    if (latchUnavailable) throw new Error('Local uncertainty latch unavailable; submission not sent');
    const begun = await request('/api/guard/begin', intent); guard = begun.operation;
    if (guard.status !== 'SUBMITTING') { localUncertain = false; persistLatches(); report(`Operation ${guard.status}; no new submission.`, begun); return; }
    // Server ledger is durable before this raw submission. A local latch covers response loss.
    localUncertain = true; persistLatches();
    if (latchUnavailable) throw new Error('Local uncertainty latch unavailable; submission not sent');
    report('Publishing…');
    await request('/api/publish', {...value, drop_after_commit: $('drop').checked});
    await verifyOnePost();
  } catch (error) {
    if (error.operation) { guard = error.operation; if (guard.status === 'PREPARED') { localUncertain = false; persistLatches(); } }
    if (guard && ['SUBMITTING','NEEDS_RECONCILIATION'].includes(guard.status)) { localUncertain = true; persistLatches(); try { const value = await request('/api/guard/uncertain', {operation_id: $('operation').value, reference: 'synthetic-publish-or-readback-outcome-unknown'}); guard = value.operation; } catch (ignore) { /* Durable existing guard still blocks. */ } }
    report(`Publish blocked or uncertain: ${error.message}. Find posts and read back; do not submit again.`);
  } finally { pending--; render(); mountFile(); }
}
$('mode').onchange = () => { const value = html(lastMode); for (const [name, mode] of [['rich','rich'],['frame','iframe'],['plain','plain']]) $(name).hidden = $('mode').value !== mode; $('toolbar').hidden = $('mode').value === 'plain'; setBody(value); lastMode = $('mode').value; renderAttachments(); render(); };
$('title').oninput = render; $('operation').oninput = loadGuard; $('rich').oninput = render; $('plain').oninput = render;
$('frame').onload = () => { editor('iframe').oninput = render; };
$('bold').onclick = () => { editor().focus(); editor().ownerDocument.execCommand('bold'); render(); };
$('link').onclick = () => { const href = $('link-url').value; if (!/^https?:\/\//i.test(href)) { report('Use an http or https link URL'); return; } editor().focus(); editor().ownerDocument.execCommand('createLink', false, href); render(); };
$('find').onclick = async () => { pending++; render(); try { await verifyOnePost(); } catch (error) { report(`Readback verification failed: ${error.message}; existing guard remains blocked.`); } finally { pending--; render(); } };
mountFile(); loadGuard();
