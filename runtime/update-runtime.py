"""Check the official neo feed and prepare a signed, D-owned update candidate.

Does not run the installer or change the active browser/profile/runtime plan.
Promotion follows prepare-runtime.ps1 verification and a safe restart boundary.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx
from Crypto.Signature import eddsa
from workspace_storage import atomic_write_json

FEED = "https://cdn.browseros.com/appcast-claw-win.xml"
PUBLIC_KEY = "LzQmcNuTsdB3/dsivo0eeN+jPfDoriRHAkkEJcfFs2A="
NS = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def version(value):
    parts = value.split('.')
    if not 3 <= len(parts) <= 4 or not all(p.isdecimal() for p in parts):
        raise ValueError("Invalid release version")
    return tuple(map(int, parts)) + (0,) * (4 - len(parts))


def release(xml):
    items = []
    for item in ET.fromstring(xml).findall('./channel/item'):
        enc = item.find('enclosure')
        if enc is None or enc.get(NS + 'os') != 'windows':
            continue
        name = item.findtext(NS + 'shortVersionString')
        version(name)
        url = enc.get('url', '')
        parsed = urlparse(url)
        expected = f'/releases/browserclaw/{name}/win/BrowserOS_neo_v{name}_x64_installer.exe'
        if parsed.scheme not in ('https', 'http') or parsed.netloc != 'cdn.browseros.com' or parsed.path != expected or parsed.query or parsed.fragment:
            raise ValueError("Unexpected installer source")
        signature = enc.get(NS + 'edSignature', '')
        if len(base64.b64decode(signature, validate=True)) != 64:
            raise ValueError("Missing release signature")
        size = int(enc.get('length', '0'))
        if not 1_000_000 <= size <= 1_000_000_000:
            raise ValueError("Unexpected installer length")
        items.append(dict(version=name, url='https://cdn.browseros.com' + parsed.path,
                          bytes=size, ed_signature=signature))
    if not items:
        raise ValueError("No signed Windows x64 neo release")
    return max(items, key=lambda i: version(i['version']))


def verify(path, info):
    if path.stat().st_size != info['bytes']:
        raise ValueError("Installer length mismatch")
    payload = path.read_bytes()
    key = eddsa.import_public_key(base64.b64decode(PUBLIC_KEY))
    eddsa.new(key, 'rfc8032').verify(payload, base64.b64decode(info['ed_signature']))
    return hashlib.sha256(payload).hexdigest()


def task_directory(task_root):
    if task_root is None:
        raise ValueError('--prepare requires the explicit installation scratch subdirectory')
    raw_root = task_root.absolute()
    if any(p.is_symlink() or p.is_junction() for p in (raw_root, *raw_root.parents)):
        raise ValueError('Reparse task path')
    root = raw_root.resolve()
    allowed = Path(json.loads(Path(__file__).with_name('runtime-plan.json').read_text(encoding='utf-8'))['scratch_root']).resolve()
    if not root.is_relative_to(allowed) or root == allowed:
        raise ValueError('Use a installation scratch subdirectory')
    root.mkdir(parents=True, exist_ok=True)
    return root


def download_installer(client, info, root):
    installer = root / Path(urlparse(info['url']).path).name
    if not installer.exists():
        part = installer.with_suffix('.part')
        with client.stream('GET', info['url']) as stream:
            stream.raise_for_status()
            if str(stream.url) != info['url']:
                raise ValueError('Unexpected installer redirect')
            total = 0
            with part.open('xb') as output:
                for block in stream.iter_bytes():
                    total += len(block)
                    if total > info['bytes']:
                        raise ValueError('Installer exceeds signed length')
                    output.write(block)
        verify(part, info)
        part.rename(installer)
    return installer


def prepare_candidate(client, plan, info, task_root):
    root = task_directory(task_root)
    installer = download_installer(client, info, root)
    digest = verify(installer, info)
    candidate = dict(plan, version=info['version'], installer_url=info['url'],
                     installer_bytes=info['bytes'], installer_sha256=digest,
                     proposed_runtime_root=str(Path(plan['proposed_runtime_root']).parent / info['version']),
                     runtime_verified=False, provisioning_status='PREPARED_NOT_ACTIVE')
    # Download verification does not verify the new payload's Chromium/server or
    # transfer the active runtime's acceptance evidence to this candidate.
    candidate['chromium_version'] = None
    candidate['provisioning_method'] = 'Signed installer downloaded; extraction and compatibility validation pending'
    candidate['first_run_plan'] = 'Dedicated-profile controls and agent-config preservation require candidate validation'
    candidate['previous_server_override'] = plan.get('server_override')
    candidate['server_override'] = {'enabled': False, 'activation_status': 'UNVERIFIED_NOT_ACTIVE'}
    candidate_path = root / 'candidate-plan.json'
    atomic_write_json(candidate_path, candidate, backup_count=0)
    return dict(installer=str(installer), sha256=digest, ed25519_verified=True,
                candidate_plan=str(candidate_path), active_plan_changed=False)


def check_update(client, plan, *, prepare=False, task_root=None):
    response = client.get(FEED)
    response.raise_for_status()
    if str(response.url) != FEED:
        raise ValueError('Unexpected feed redirect')
    info = release(response.content)
    update = version(info['version']) > version(plan['version'])
    result = dict(installed=plan['version'], available=info['version'], update_available=update,
                  feed=FEED, active_runtime=plan['proposed_runtime_root'])
    if prepare and update:
        result.update(prepare_candidate(client, plan, info, task_root))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, default=Path(__file__).with_name('runtime-plan.json'))
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--task-root', type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        result = check_update(client, plan, prepare=args.prepare, task_root=args.task_root)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
