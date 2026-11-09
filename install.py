"""Explicit-root Windows installation. No registry, shared Python or browser changes."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Use an absolute path without traversal')
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError(f'Reparse component: {item}')
    if path.parent == path:
        raise ValueError('A drive root is not an installation root')
    return path


def powershell(code):
    encoded = base64.b64encode(code.encode('utf-16-le')).decode('ascii')
    environment = {k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    result = subprocess.run(['powershell.exe','-NoProfile','-EncodedCommand',encoded],
        capture_output=True, env=environment, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8',errors='replace'))
    return result.stdout.decode('utf-8-sig')


def signature(path, publisher):
    literal = str(path).replace("'", "''")
    text = powershell("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
        f"$s=Get-AuthenticodeSignature -LiteralPath '{literal}';"
        "[pscustomobject]@{status=[string]$s.Status;publisher=if($s.SignerCertificate){"
        "$s.SignerCertificate.GetNameInfo('SimpleName',$false)}else{''}}|ConvertTo-Json -Compress")
    data = json.loads(text)
    if data != {'status':'Valid','publisher':publisher}:
        raise ValueError('Official Authenticode signature/publisher mismatch')


def verify_source():
    manifest = json.loads((HERE/'source-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe source manifest member')
        child = safe_path(HERE/relative)
        if digest(child) != expected:
            raise ValueError(f'Source integrity mismatch: {name}')
    return digest(HERE/'source-manifest.json'), manifest


def install(args):
    if sys.platform != 'win32' or sys.version_info[:2] != (3,13):
        raise RuntimeError('This candidate requires Windows x64 and Python 3.13')
    import struct
    if struct.calcsize('P') != 8:
        raise RuntimeError('Use x64 Python')
    import ctypes
    system=ctypes.create_unicode_buffer(32768)
    length=ctypes.windll.kernel32.GetSystemDirectoryW(system,len(system))
    if not 0 < length < len(system) or not (Path(system.value)/'vcruntime140.dll').is_file():
        raise RuntimeError('Microsoft Visual C++ v14 x64 Redistributable is required (VCRUNTIME140.dll in System32). Install it from the official Microsoft download page, then rerun. No shared runtime was changed.')
    source_id, source_manifest = verify_source()
    root = safe_path(args.root)
    pins = json.loads((HERE/'pins.json').read_text(encoding='utf-8'))
    ports = [args.proxy_port,args.cdp_port,args.backend_port]
    if len(set(ports)) != 3 or any(not 1024 <= p <= 65535 for p in ports):
        raise ValueError('Use three distinct unprivileged ports')
    record_path = root/'installation.json'
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding='utf-8'))
        if record.get('status') != 'installed' or record.get('source_id') != source_id or record.get('ports') != ports:
            raise ValueError('Existing installation differs; preserve it and use a new candidate root')
        plan = json.loads((root/'runtime/runtime-plan.json').read_text(encoding='utf-8'))
        if (Path(plan['proposed_runtime_root']) != root/'browser' or
                Path(plan['profile_root']) != root/'local/profile' or
                digest(plan['server_override']['executable']) != pins['server_override']['sha256']):
            raise ValueError('Existing installation identity changed')
        if (Path(plan['python_executable']) != root/'.venv/Scripts/python.exe' or
                not Path(plan['python_executable']).is_file() or
                digest(root/'browser/Chrome-bin/chrome.exe') != record.get('browser_sha256')):
            raise ValueError('Existing browser/Python identity changed')
        for name, expected in source_manifest.items():
            if name.startswith('runtime/') and digest(safe_path(root/Path(name))) != expected:
                raise ValueError(f'Existing runtime source changed: {name}')
        print(json.dumps({'status':'already-installed','browserStarted':False}))
        return
    if root.exists() and any(root.iterdir()):
        raise ValueError('Refusing a nonempty root without a successful installation record')
    server = safe_path(args.server or HERE/'assets/browseros-claw-server.exe')
    if digest(server) != pins['server_override']['sha256']:
        raise ValueError('Patched server does not match the qualified binary pin')
    # Refuse mixing a source checkout with generated state or a different installation.
    if root == HERE or root in HERE.parents or HERE in root.parents:
        raise ValueError('Install outside the source tree')
    root.mkdir(parents=True, exist_ok=True)
    scratch = root/'local/scratch'
    scratch.mkdir(parents=True)
    environment = {k:v for k,v in os.environ.items() if k.upper() not in ('PYTHONPATH','PYTHONHOME','PSMODULEPATH')}
    environment.update(TEMP=str(scratch),TMP=str(scratch),TMPDIR=str(scratch),PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable,'-B','-m','venv',str(root/'.venv')],env=environment,check=True,timeout=120)
    python = root/'.venv/Scripts/python.exe'
    command = [str(python),'-B','-m','pip','install','--disable-pip-version-check','--no-cache-dir','--require-hashes','--only-binary=:all:']
    if args.wheelhouse:
        command += ['--no-index','--find-links',str(safe_path(args.wheelhouse))]
    command += ['-r',str(HERE/'requirements.txt')]
    subprocess.run(command,env=environment,check=True,timeout=300)
    # Copy only verified source members, never ignored local plans or profiles.
    for name, expected in source_manifest.items():
        if not name.startswith('runtime/'):
            continue
        target = safe_path(root/Path(name))
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(safe_path(HERE/Path(name)),target)
        if digest(target) != expected:
            raise ValueError(f'Installed source differs from manifest: {name}')
    shutil.copyfile(HERE/'pins.json',root/'pins.json')
    shutil.copyfile(HERE/'LICENSE',root/'LICENSE')
    shutil.copyfile(HERE/'NOTICE',root/'NOTICE')
    resource = root/'server/resources/bin'
    resource.mkdir(parents=True)
    shutil.copyfile(server,resource/'browseros-claw-server.exe')
    plan = dict(pins)
    plan.pop('patches')
    plan.update(schema_version=1,proposed_runtime_root=str(root/'browser'),
        profile_root=str(root/'local/profile'),server_state_root=str(root/'local/server'),
        ledger_root=str(root/'local/data'),scratch_root=str(scratch),python_executable=str(python),
        installation_id=str(uuid.uuid4()),mcp_endpoint=f'http://127.0.0.1:{args.proxy_port}/mcp',
        cdp_port=args.cdp_port,backend_port=args.backend_port,runtime_verified=False,runtime_mode='DIRECT_LOOPBACK')
    plan['server_override'] = dict(pins['server_override'],enabled=True,
        executable=str(resource/'browseros-claw-server.exe'),resources_root=str(resource.parent))
    (root/'runtime/runtime-plan.json').write_text(json.dumps(plan,indent=2)+'\n',encoding='utf-8')
    # The official --disable-browseros-server switch leaves CDP enabled but
    # disables the all-interface proxy and managed server. Bootstrap the pinned
    # sidecar schema for our existing exact-identity loopback recovery launcher.
    sidecar = root/'local/profile/.browseros'
    sidecar.mkdir(parents=True)
    (sidecar/'config.json').write_text(json.dumps({
        'directories':{'execution':str(sidecar),'resources':str(resource.parent)},
        'ports':{'proxy':args.proxy_port,'server':args.backend_port,'cdp':args.cdp_port},
        'flags':{'allow_remote_in_mcp':False}}),encoding='utf-8')
    (sidecar/'server.lock').touch(exist_ok=False)
    installer = safe_path(args.installer) if args.installer else scratch/'official-installer.exe'
    if not args.installer:
        subprocess.run([str(python),'-B',str(HERE/'download-payload.py'),pins['installer_url'],
            str(installer),str(pins['installer_bytes']),pins['installer_sha256']],
            env=environment,check=True,timeout=630)
    if installer.stat().st_size != pins['installer_bytes'] or digest(installer) != pins['installer_sha256']:
        raise ValueError('Official installer size/SHA mismatch')
    signature(installer,pins['authenticode_publisher'])
    subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
        str(root/'runtime/prepare-runtime.ps1'),'-Installer',str(installer),'-ScratchRoot',str(scratch/'extract'),
        '-Execute'],env=environment,check=True,timeout=180)
    # Both templates are local outputs. Never rewrite global client configuration.
    templates = root/'local/templates'
    templates.mkdir(parents=True)
    bridge = str(root/'runtime/neo_mcp_bridge.py')
    codex = '\n'.join(('[mcp_servers.browseros-neo]', 'command = '+json.dumps(str(python)),
        'args = '+json.dumps(['-B','-X','utf8',bridge]), 'startup_timeout_sec = 60',''))
    (templates/'codex.toml').write_text(codex,encoding='utf-8')
    (templates/'agy-mcp.json').write_text(json.dumps({'mcpServers':{'browseros-neo':{
        'command':str(python),'args':['-B','-X','utf8',bridge,'--client','agy']}}},indent=2),encoding='utf-8')
    shutil.copyfile(HERE/'SKILL.md',templates/'SKILL.md')
    shutil.copyfile(root/'runtime/neo-cleanup-hooks.json',templates/'neo-cleanup-hooks.json')
    # This exact extraction tree was created here and is no longer needed.
    # Preserve it on failure; never delete a supplied offline installer.
    extraction = scratch/'extract'
    if extraction.exists():
        safe_path(extraction)
        for item in extraction.rglob('*'):
            safe_path(item)
        shutil.rmtree(extraction)
    if not args.installer:
        safe_path(installer).unlink()
    record = {'status':'installed','source_id':source_id,'ports':ports,'version':pins['version'],
        'server_version':pins['server_override']['version'],'browserStarted':False,
        'browser_sha256':digest(root/'browser/Chrome-bin/chrome.exe')}
    record_path.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--installer',type=Path)
    parser.add_argument('--server',type=Path)
    parser.add_argument('--wheelhouse',type=Path)
    parser.add_argument('--proxy-port',type=int,default=9010)
    parser.add_argument('--cdp-port',type=int,default=9111)
    parser.add_argument('--backend-port',type=int,default=9210)
    install(parser.parse_args())
