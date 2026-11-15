"""Runs inside the solve container: time `sh /work/run.sh /data/root` and record the result in /out."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

timeout = float(sys.argv[1])
with open("/out/stdout", "wb") as out, open("/tmp/.stderr", "wb") as err:
    t0 = time.perf_counter()
    p = subprocess.Popen(["sh", "/work/run.sh", "/data/root"], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                         cwd="/work", start_new_session=True)
    fired = threading.Event()

    def kill() -> None:
        fired.set()
        os.killpg(p.pid, signal.SIGKILL)

    timer = threading.Timer(timeout, kill)  # wait() with a timeout polls in up-to-50 ms sleeps; this doesn't
    timer.start()
    code = p.wait()
    seconds = time.perf_counter() - t0
    timer.cancel()
    timed_out = fired.is_set()
with open("/tmp/.stderr", "rb") as f:
    f.seek(max(0, os.path.getsize("/tmp/.stderr") - 20000))
    tail = f.read().decode("utf-8", "replace")
json.dump({"seconds": seconds, "exit": code, "timeout": timed_out, "stderr_tail": tail}, open("/out/result.json", "w"))
