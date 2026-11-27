"""
restart_apps.py  --  clean restart of the local demo apps.

Kills WHATEVER is listening on the app ports first (on Windows, Streamlit
runs under python.exe, so killing by image name silently misses it and
instances pile up serving stale code), then launches one server per app.

Usage:  python restart_apps.py
"""

import re
import subprocess
import sys
import time

APPS = [("app_superstore.py", 8501),
        ("app_world_indicators.py", 8502),
        ("app_regional_analysis.py", 8503),
        ("app_globalsalesdashboard.py", 8504)]


def pids_on_port(port):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            m = re.search(r"(\d+)\s*$", line)
            if m:
                pids.add(m.group(1))
    return pids


def main():
    for _, port in APPS:
        for pid in pids_on_port(port):
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True)
    time.sleep(2)
    for app, port in APPS:
        subprocess.Popen([sys.executable, "-m", "streamlit", "run", app,
                          "--server.headless", "true",
                          "--server.port", str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"started {app} -> http://localhost:{port}")
    time.sleep(20)
    for _, port in APPS:
        n = len(pids_on_port(port))
        print(f"port {port}: {n} listener{'s' if n != 1 else ''} "
              f"{'OK' if n == 1 else '!! expected exactly 1'}")


if __name__ == "__main__":
    main()
