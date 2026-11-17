"""Launch the approved dedicated browser with Windows nonactivating startup."""
import json
import subprocess
import sys

launch = json.load(sys.stdin)
startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
with open(launch["log"], "xb") as log:
    child = subprocess.Popen(
        [launch["executable"], *launch["arguments"]],
        cwd=launch["directory"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
print(child.pid)
