"""Bounded official-payload download, run only inside the private environment."""
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys
import time
import httpx


def download(url, path, size, sha):
    if not url.startswith('https://cdn.browseros.com/releases/browserclaw/'):
        raise ValueError('Unexpected payload source')
    deadline = time.monotonic() + 600
    chunk_size = 4 * 1024 * 1024
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=20) as client:
        def chunk(start):
            end = min(start + chunk_size, size) - 1
            with client.stream('GET',url,headers={'Range':f'bytes={start}-{end}'}) as response:
                if (response.status_code != 206 or str(response.url) != url or
                        response.headers.get('content-range') != f'bytes {start}-{end}/{size}'):
                    raise ValueError('Payload range/source mismatch')
                raw = bytearray()
                for block in response.iter_bytes():
                    if len(raw) + len(block) > end-start+1 or time.monotonic() > deadline:
                        raise ValueError('Payload exceeds range or total deadline')
                    raw.extend(block)
                if len(raw) != end-start+1:
                    raise ValueError('Truncated payload range')
                return start, raw
        # Bounded memory and concurrency. Exact ranges are verified, then the
        # complete installer is hashed; a part is never accepted independently.
        with Path(path).open('xb') as target, ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(chunk,start) for start in range(0,size,chunk_size)]
            try:
                for job in as_completed(jobs,timeout=610):
                    offset, raw = job.result()
                    target.seek(offset)
                    target.write(raw)
            except BaseException:
                for job in jobs:
                    job.cancel()
                raise
    with Path(path).open('rb') as stream:
        actual = hashlib.file_digest(stream,'sha256').hexdigest()
    if Path(path).stat().st_size != size or actual != sha:
        raise ValueError('Payload does not match pinned size/SHA')


if __name__ == '__main__':
    download(sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4])
