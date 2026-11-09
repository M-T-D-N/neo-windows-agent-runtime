"""Explicit maintainer hash refresh of existing source members, never local data."""
import argparse
import hashlib
import json
from pathlib import Path

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--write',action='store_true')
    args=p.parse_args()
    root=Path(__file__).resolve().parent
    file=root/'source-manifest.json'
    previous=json.loads(file.read_text(encoding='utf-8'))
    current={}
    for name in previous:
        member=Path(name)
        if member.is_absolute() or '..' in member.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe source member')
        source=root/member
        if any(x.is_symlink() or x.is_junction() for x in (source,*source.parents)):
            raise ValueError('Reparse source member')
        current[name]=hashlib.sha256(source.read_bytes()).hexdigest()
    changed=[name for name in previous if previous[name]!=current[name]]
    if args.write:
        file.write_text(json.dumps(current,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'changed':changed,'written':args.write}))
