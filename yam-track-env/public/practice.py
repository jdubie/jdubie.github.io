"""Run controller.py on a practice reference through the arm's socket, the same way it will be scored.

  python practice.py gravity_holds            (any name in references/)
  python practice.py recorded_0 --controller my_other_controller.py

Prints the tracking error and writes practice_runs/<name>.json: the reference, every observation and
every command, frame by frame.
"""

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("name")
ap.add_argument("--controller", default="controller.py")
ap.add_argument("--sock", default="/sim/sim.sock")
a = ap.parse_args()

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(a.sock)
f = s.makefile("rw", buffering=1)
json.loads(f.readline())  # hello
f.write(json.dumps({"type": "trial", "name": a.name}) + "\n")
start = json.loads(f.readline())
if start.get("type") != "trial_start":
    sys.exit(f"could not start the trial: {start}")

here = Path(__file__).resolve().parent
child = subprocess.Popen([sys.executable, str(here / "run_controller.py"), a.controller],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
child.stdin.write(json.dumps(start) + "\n")
child.stdin.flush()
obs, cmds, result = [start["obs"]], [], None
limp = {"type": "step", "q": [0.0] * 6}
while result is None:
    line = child.stdout.readline()
    if line:
        cmd = json.loads(line)
    else:  # the controller stopped early: the drives go limp for the rest of the reference
        cmd = limp
    cmds.append(cmd)
    f.write(json.dumps(cmd) + "\n")
    reply = json.loads(f.readline())
    if reply["type"] == "error":
        print("arm rejected a command:", reply["msg"], file=sys.stderr)
        f.write(json.dumps(limp) + "\n")
        reply = json.loads(f.readline())
    obs.append(reply)
    if line and child.poll() is None:
        child.stdin.write(json.dumps(reply) + "\n")
        child.stdin.flush()
    if reply.get("frame") == start["n_frames"]:
        result = json.loads(f.readline())
try:
    child.stdin.close()
except BrokenPipeError:
    pass
try:
    child.wait(timeout=10)
except subprocess.TimeoutExpired:
    child.kill()

print(f"{a.name}: RMS tracking error {result['rms_mm']:.3f} mm   max {result['max_mm']:.2f} mm")
print("per point RMS mm:", result["per_point_rms_mm"])
out = Path("practice_runs")
out.mkdir(exist_ok=True)
(out / f"{a.name}.json").write_text(json.dumps({"ref": start["ref"], "obs": obs, "cmds": cmds, "result": result}))
print(f"wrote practice_runs/{a.name}.json")
