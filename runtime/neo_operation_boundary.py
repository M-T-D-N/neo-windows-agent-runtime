"""Profile-wide shared browser operations and exclusive offline administration.

LockFileEx semantics: https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex
No lease, daemon or stale marker: the OS owns the held byte-range lock.
"""
import asyncio
import ctypes
from functools import lru_cache
import os
from pathlib import Path
import time

from workspace_storage import ensure_no_reparse_components


@lru_cache(maxsize=1)
def _windows_api():
    from ctypes import wintypes
    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal',ctypes.c_size_t),('InternalHigh',ctypes.c_size_t),
            ('Offset',wintypes.DWORD),('OffsetHigh',wintypes.DWORD),('hEvent',wintypes.HANDLE)]
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.LockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
        wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(Overlapped)]
    api.LockFileEx.restype=wintypes.BOOL
    api.UnlockFileEx.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.DWORD,
        wintypes.DWORD,ctypes.POINTER(Overlapped)]
    api.UnlockFileEx.restype=wintypes.BOOL
    return api,Overlapped


class OperationBoundary:
    def __init__(self,path,*,exclusive=False,timeout=45):
        ensure_no_reparse_components(Path(path))
        self.path=Path(path).resolve(strict=False)
        self.exclusive=exclusive
        self.timeout=timeout
        self.stream=None
        self.held=False
        self.owner_pid=None

    def _try_acquire(self):
        if self.stream is None:
            ensure_no_reparse_components(self.path)
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.stream=self.path.open('a+b')
        if os.name=='nt':
            import msvcrt
            api,Overlapped=_windows_api()
            self.overlapped=Overlapped()
            self.handle=msvcrt.get_osfhandle(self.stream.fileno())
            if not api.LockFileEx(self.handle,1|(2 if self.exclusive else 0),0,1,0,ctypes.byref(self.overlapped)):
                error=ctypes.get_last_error()
                if error==33:return False
                raise ctypes.WinError(error)
        else:
            import fcntl
            try:fcntl.flock(self.stream.fileno(),fcntl.LOCK_NB|(fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH))
            except BlockingIOError:return False
        self.held=True
        self.owner_pid=os.getpid()
        return True

    def require_exclusive(self,path):
        if (not self.held or not self.exclusive or self.owner_pid!=os.getpid()
                or self.path!=Path(path).resolve(strict=False)):
            raise ValueError('Hold the exact profile exclusive boundary throughout closure verification and reset')

    def close(self):
        try:
            if self.held:
                if os.name=='nt':
                    api,_=_windows_api()
                    if not api.UnlockFileEx(self.handle,0,1,0,ctypes.byref(self.overlapped)):
                        raise ctypes.WinError(ctypes.get_last_error())
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
        finally:
            self.held=False
            self.owner_pid=None
            if self.stream is not None:self.stream.close();self.stream=None

    def __enter__(self):
        deadline=time.monotonic()+self.timeout
        try:
            while not self._try_acquire():
                if time.monotonic()>=deadline:raise TimeoutError('Browser operation boundary is busy; preserve current work')
                time.sleep(.025)
            return self
        except BaseException:self.close();raise

    def __exit__(self,*_):self.close()

    async def __aenter__(self):
        deadline=time.monotonic()+self.timeout
        try:
            while not self._try_acquire():
                if time.monotonic()>=deadline:raise TimeoutError('Browser operation boundary is busy; preserve current work')
                await asyncio.sleep(.025)
            return self
        except BaseException:self.close();raise

    async def __aexit__(self,*_):self.close()
