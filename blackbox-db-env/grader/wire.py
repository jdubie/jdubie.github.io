"""Talk to a server in the variant's protocol, record what comes back, and score a reply against the oracle's.

A workload is a list of steps. A step sends some bytes (or nothing), then reads replies until a stop rule:
  Z      until ReadyForQuery (or the connection closes)
  close  until the connection closes
  count  until `n` messages
  idle   (oracle) until 0.4 s of silence; (candidate) until as many messages as the oracle sent
  raw1   exactly one raw byte (the reply to an encryption request)
Steps may also open a side connection that sends a cancel request for this session after `cancel` seconds.
"""

from __future__ import annotations

import difflib
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oracle"))
from variant import Codec, Malformed  # noqa: E402

CAND_STEP_TIMEOUT = 5.0
ORACLE_STEP_TIMEOUT = 30.0


class Conn:
    def __init__(self, host: str, port: int, codec: Codec, timeout: float):
        self.codec, self.timeout = codec, timeout
        self.buf = bytearray()
        self.closed = False
        self.keydata: tuple[int, int] | None = None
        self.sock: socket.socket | None = None
        try:
            self.sock = socket.create_connection((host, port), timeout=3)
        except OSError:
            self.closed = True

    def send(self, b: bytes) -> None:
        if self.closed or not b:
            return
        try:
            self.sock.sendall(b)
        except OSError:
            self.closed = True

    def _fill(self, deadline: float, idle: float | None = None) -> bool:
        """Read some bytes; False on timeout/idle/EOF."""
        if self.closed:
            return False
        left = deadline - time.time()
        if idle is not None:
            left = min(left, idle)
        if left <= 0:
            return False
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(1 << 20)
        except socket.timeout:
            return False
        except OSError:
            chunk = b""
        if not chunk:
            self.closed = True
            return False
        self.buf += chunk
        return True

    def read(self, mode: str, n: int = 0, timeout: float | None = None) -> dict:
        """Messages per the stop rule: {"msgs": [[type, body hex]], "closed", "timeout", "junk"}."""
        deadline = time.time() + (timeout or self.timeout)
        msgs: list[list[str]] = []
        junk = None
        if mode == "raw1":
            while not self.buf and self._fill(deadline):
                pass
            if self.buf:
                msgs.append(["raw", bytes(self.buf[:1]).hex()])
                del self.buf[:1]
            return {"msgs": msgs, "closed": self.closed and not self.buf, "timeout": not msgs and not self.closed, "junk": None}
        timed_out = False
        while True:
            # drain complete frames
            while True:
                try:
                    r = self.codec.split(self.buf, 0, True)
                except Malformed:
                    r = None
                    junk = bytes(self.buf).hex()
                    self.buf.clear()
                if r is None:
                    break
                t, body, k = r
                del self.buf[:k]
                msgs.append([t, body.hex()])
                if self.codec.back_kind.get(t) == "keydata" and len(body) == 8:
                    m = self.codec.decode("b:keydata", body)
                    self.keydata = (m["pid"], m["key"])
            if junk is not None:
                break
            last = self.codec.back_kind.get(msgs[-1][0]) if msgs else None
            if mode == "Z" and last == "ready":
                break
            if mode == "Zn" and sum(self.codec.back_kind.get(m[0]) == "ready" for m in msgs) >= n:
                break
            if mode == "count" and len(msgs) >= n:
                break
            if mode == "idle_n" and len(msgs) >= n:
                break
            if self.closed:
                break
            if not self._fill(deadline, 0.4 if mode == "idle" else None):
                if not self.closed:
                    timed_out = mode != "idle"
                break
        if self.buf and self.closed and junk is None:
            junk = bytes(self.buf).hex()
            self.buf.clear()
        return {"msgs": msgs, "closed": self.closed, "timeout": timed_out, "junk": junk}

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.closed = True


def send_cancel(host: str, port: int, codec: Codec, spec: dict, keydata: tuple[int, int] | None, delay: float) -> None:
    time.sleep(delay)
    if keydata is None:
        return
    try:
        s = socket.create_connection((host, port), timeout=3)
        s.settimeout(2)
        try:
            s.recv(4096)  # the greeting
        except OSError:
            pass
        body = codec.u32(spec["wire"]["cancel_magic"]) + codec.i32(keydata[0]) + codec.i32(keydata[1])
        s.sendall(codec.frame_untyped(body))
        try:
            s.recv(4096)
        except OSError:
            pass
        s.close()
    except OSError:
        pass


def run_workload(host: str, port: int, spec: dict, wl: dict, oracle: list | None = None) -> list:
    """Run one workload; returns one record per step. With `oracle`, read like a candidate (stop rules
    sized from the oracle's transcript, short timeouts, give up after two silent steps)."""
    codec = Codec(spec)
    timeout = CAND_STEP_TIMEOUT if oracle is not None else ORACLE_STEP_TIMEOUT
    c = Conn(host, port, codec, timeout)
    out = []
    silent = 0
    try:
        for i, st in enumerate(wl["steps"]):
            if oracle is not None and silent >= 2:
                out.append({"msgs": [], "closed": c.closed, "timeout": True, "junk": None, "skipped": True})
                continue
            canceller = None
            if st.get("cancel") is not None:
                canceller = threading.Thread(target=send_cancel, args=(host, port, codec, spec, c.keydata, st["cancel"]))
            c.send(bytes.fromhex(st["send"]) if st.get("send") else b"")
            if canceller:
                canceller.start()
            mode, n = st["until"], st.get("n", 0)
            if oracle is not None and mode == "idle":
                mode, n = "idle_n", len(oracle[i]["msgs"])
            r = c.read(mode, n, (timeout * 2) if st.get("cancel") is not None else None)
            if canceller:
                canceller.join()
            silent = silent + 1 if (r["timeout"] and not r["msgs"]) else 0
            out.append(r)
    finally:
        c.close()
    return out


# ------------------------------------------------------------------ scoring

def _norm(codec: Codec, m: list) -> list:
    t, body = m
    if codec.back_kind.get(t) == "keydata":
        return [t, "00" * (len(body) // 2)]  # random per connection: only its shape is compared
    return m


def _fields(codec: Codec, m: list) -> list | None:
    t, body = m
    kind = codec.back_kind.get(t)
    if kind is None:
        return None
    try:
        d = codec.decode("b:" + kind, bytes.fromhex(body))
    except (Malformed, Exception):  # noqa: BLE001 - an undecodable candidate message just earns less
        return None
    if kind == "rowdesc":
        return [(i, k, v) for i, f in enumerate(d["fields"]) for k, v in sorted(f.items())] + [("n", len(d["fields"]))]
    if kind == "datarow":
        return list(enumerate(d["values"])) + [("n", len(d["values"]))]
    if kind in ("error", "notice"):
        return sorted(d["fields"].items())
    return sorted((k, repr(v)) for k, v in d.items())


def msg_credit(codec: Codec, o: list, c: list) -> float:
    """1 for an identical message; up to 0.5 for the right message type with some fields right."""
    if o == c:
        return 1.0
    if o[0] != c[0]:
        return 0.0
    fo, fc = _fields(codec, o), _fields(codec, c)
    if not fo or fc is None:
        return 0.0
    same = len(set(map(repr, fo)) & set(map(repr, fc)))
    return 0.5 * same / max(len(fo), len(fc))


def step_score(codec: Codec, o: dict, c: dict) -> tuple[float, int]:
    om = [_norm(codec, m) for m in o["msgs"]]
    cm = [_norm(codec, m) for m in c["msgs"]] if not c.get("skipped") else []
    # an expected close counts as one more item to get right
    if o.get("closed") and not om:
        om_close = 1
    else:
        om_close = 1 if o.get("closed") else 0
    denom = max(len(om), len(cm)) + om_close
    if denom == 0:
        return 0.0, 0
    sm = difflib.SequenceMatcher(None, [m[0] for m in om], [m[0] for m in cm], autojunk=False)
    credit = 0.0
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            credit += msg_credit(codec, om[a + k], cm[b + k])
    if om_close and c.get("closed") and not c.get("skipped"):
        credit += 1
    return credit, denom


def workload_score(spec: dict, wl: dict, oracle: list, cand: list) -> dict:
    codec = Codec(spec)
    first = wl.get("score_from", 0)
    gate = wl.get("gate")  # the login step: unless it reached ReadyForQuery, nothing after it scores
    gm = cand[gate]["msgs"] if gate is not None else []
    if gate is not None and not (gm and codec.back_kind.get(gm[-1][0]) == "ready"):
        credit = 0.0
        denom = sum(step_score(codec, oracle[i], oracle[i])[1] for i in range(first, len(oracle)))
        return {"score": 0.0, "credit": 0.0, "denom": denom, "gated": True}
    credit, denom = 0.0, 0
    for i in range(first, len(oracle)):
        c, d = step_score(codec, oracle[i], cand[i])
        credit += c
        denom += d
    return {"score": credit / denom if denom else 0.0, "credit": round(credit, 3), "denom": denom}
