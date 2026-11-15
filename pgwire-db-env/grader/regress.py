"""PostgreSQL's own regression suite (src/test/regress, REL 16.16), run through real psql, scored per statement.

Equivalent to `pg_regress --schedule=parallel_schedule` run serially against one server, with three changes
so that credit can be given statement by statement and only for what a client can observe:

  1. Every test file is split into units (one SQL statement, or one psql meta-command, a COPY ... FROM stdin
     together with its data, or a statement ended by \\gset/\\gexec/...). After each unit the harness sends
     `\\echo` with a marker and psql's :SQLSTATE, so the output of every unit is captured separately.
  2. psql runs with VERBOSITY=terse. Error message text is dropped; an error is compared by its SQLSTATE
     class. NOTICE/WARNING text, result tables, command tags and meta-command output (\\d etc.) are compared
     exactly, except that the rows of a query without a top-level ORDER BY may come in any order.
  3. Server-side file access is turned into client-side COPY (`COPY t FROM :'filename'` -> `\\copy t FROM
     '<path>'`), so the data files travel over the COPY protocol instead of being read from the server's disk.

Units are dropped from scoring when they cannot apply (EXPLAIN plan text, C functions from regress.so,
server filesystem paths) or when PostgreSQL itself answers differently on two fresh runs. A unit is scored
only if it is observable: its PostgreSQL output has rows, an error, a notice, meta-command output or a row
count (a bare `CREATE TABLE` tag is not scored; its effect is, through later statements). As in v1, an
expected error earns credit only if the latest success-expected unit of the same file also passed.

score = mean over test files of (units passed / units scored).
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGRESS = ROOT / "pgsrc/postgresql-16.16/src/test/regress"
SCHEDULE = REGRESS / "parallel_schedule"
CLIENT_REGRESS = "/regress"
CLIENT_BUILD = "/tmp/regress-build"

EXCLUDED_TESTS = {
    "tablespace": "creates directories on the server's filesystem",
    "create_function_c": "C-language functions loaded from regress.so",
    "collate.icu.utf8": "needs an ICU UTF-8 database; skips itself under C locale",
    "collate.linux.utf8": "needs a glibc UTF-8 locale database; skips itself under C locale",
    "collate.windows.win1252": "Windows-only",
    "euc_kr": "needs an EUC_KR database",
    "opr_sanity": "consistency checks of PostgreSQL's own catalog contents (zero rows expected; trivially matched by an empty catalog)",
    "type_sanity": "same as opr_sanity",
    "misc_sanity": "same as opr_sanity",
    "oidjoins": "same as opr_sanity",
}
EXCLUDE_UNIT_PATTERNS = [
    (re.compile(r"^\s*explain\b", re.I), "EXPLAIN plan text"),
    (re.compile(r":'?(regresslib|libdir|dlsuffix)\b|regress\.so|\$libdir", re.I), "C function from regress.so"),
    (re.compile(r":'?(abs_srcdir|abs_builddir)\b", re.I), "server filesystem path"),
    (re.compile(r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|lo_export)\s*\(", re.I), "server filesystem"),
]
UNIT_TIMEOUT_S = 20
TOTAL_BUDGET_S = 45 * 60
MARK = "@@PGW"


def schedule() -> list[str]:
    out = []
    for line in SCHEDULE.read_text().splitlines():
        if line.startswith("test:"):
            out += line.split(":", 1)[1].split()
    return out


# ---------------------------------------------------------------- splitting psql input into units

G_META = re.compile(r"\\(g|gx|gset|gexec|gdesc|crosstabview|watch)\b")


def split_units(text: str) -> list[dict]:
    """Units of psql input: {"text": ..., "kind": "sql"|"meta"|"copyin"}. Text is passed to psql verbatim."""
    units: list[dict] = []
    buf: list[str] = []  # current SQL statement text (may span lines)
    lines = text.split("\n")
    i = 0
    st = {"q": None, "dollar": None, "block": 0, "paren": 0}

    def flush(kind="sql", extra=""):
        st["paren"] = 0
        st["atomic"] = 0
        s = "".join(buf) + extra
        if s.strip():
            units.append({"text": s.rstrip("\n") + "\n", "kind": kind})
        buf.clear()

    while i < len(lines):
        line = lines[i] + ("\n" if i < len(lines) - 1 else "")
        i += 1
        j = 0
        n = len(line)
        start = 0
        while j < n:
            c = line[j]
            if st["block"]:
                if line.startswith("*/", j):
                    st["block"] -= 1
                    j += 2
                    continue
                if line.startswith("/*", j):
                    st["block"] += 1
                    j += 2
                    continue
                j += 1
                continue
            if st["dollar"]:
                if line.startswith(st["dollar"], j):
                    j += len(st["dollar"])
                    st["dollar"] = None
                    continue
                j += 1
                continue
            if st["q"]:
                if c == "\\" and st["q"] == "E'":
                    j += 2
                    continue
                if c == st["q"][-1]:
                    if j + 1 < n and line[j + 1] == c:
                        j += 2
                        continue
                    st["q"] = None
                j += 1
                continue
            if line.startswith("--", j):
                break
            if line.startswith("/*", j):
                st["block"] = 1
                j += 2
                continue
            if c == "'":
                st["q"] = "E'" if st.get("scs_off") or j > 0 and line[j - 1] in "eE" and (j < 2 or not (line[j - 2].isalnum() or line[j - 2] == "_")) else "'"
                j += 1
                continue
            if c == '"':
                st["q"] = '"'
                j += 1
                continue
            if c == "$":
                m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", line[j:])
                if m and not (j > 0 and (line[j - 1].isalnum() or line[j - 1] == "_")):
                    st["dollar"] = m.group(0)
                    j += len(m.group(0))
                    continue
            if (c.isalpha() or c == "_") and not (j > 0 and (line[j - 1].isalnum() or line[j - 1] in "_$")):
                m = re.match(r"[A-Za-z_][A-Za-z0-9_$]*", line[j:])
                w = m.group(0).lower()
                # psql does not end a statement at ';' inside a CREATE FUNCTION/PROCEDURE ... BEGIN ATOMIC ... END body
                if w == "begin" and re.match(r"\s*create\s+(or\s+replace\s+)?(function|procedure)\b",
                                             re.sub(r"--[^\n]*|/\*[\s\S]*?\*/", " ", "".join(buf) + line[start:j]), re.I):
                    st["atomic"] = st.get("atomic", 0) + 1
                elif w == "case" and st.get("atomic"):
                    st["atomic"] += 1
                elif w == "end" and st.get("atomic"):
                    st["atomic"] -= 1
                j += len(w)
                continue
            if c == "(":
                st["paren"] += 1
            elif c == ")":
                st["paren"] = max(0, st["paren"] - 1)
            if c == ";" and st["paren"] == 0 and not st.get("atomic"):
                buf.append(line[start:j + 1])
                start = j + 1
                stmt = "".join(buf)
                flush()
                body_nc = re.sub(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:[^']|'')*'", " ", stmt)
                m_scs = re.search(r"\bset\s+standard_conforming_strings\s*(?:=|to)\s*'?(\w+)", body_nc, re.I)
                if m_scs:
                    st["scs_off"] = m_scs.group(1).lower() in ("off", "false", "0")
                elif re.search(r"\breset\s+(standard_conforming_strings|all)\b", body_nc, re.I):
                    st["scs_off"] = False
                blocks = len(re.findall(r"\bcopy\b[^;]*?\bfrom\s+std(?:in|out)\b", body_nc, re.I))
                if blocks and not re.match(r"^\s*\\", stmt):
                    data = []
                    while i < len(lines) and blocks:
                        dl = lines[i] + "\n"
                        i += 1
                        data.append(dl)
                        if lines[i - 1] == "\\.":
                            blocks -= 1
                    units[-1]["text"] += "".join(data)
                    units[-1]["kind"] = "copyin"
                j += 1
                continue
            if c == "\\":
                rest = line[j:]
                if rest.startswith("\\\\"):  # \\ separates meta-commands; rare in tests
                    j += 2
                    continue
                if rest.startswith("\\;"):
                    j += 2
                    continue
                before = line[start:j]
                if G_META.search(rest) and ("".join(buf) + before).strip():
                    buf.append(line[start:])
                    flush()
                else:
                    if before.strip():
                        buf.append(before)
                    if "".join(buf).strip():
                        buf.append("\n")  # psql keeps the buffer; the meta-command runs on its own
                    units.append({"text": rest.rstrip("\n") + "\n", "kind": "meta"})
                    if re.match(r"\\copy\s[\s\S]*\bfrom\s+stdin\b", rest, re.I):  # data follows inline, up to \.
                        while i < len(lines):
                            i += 1
                            units[-1]["text"] += lines[i - 1] + "\n"
                            if lines[i - 1] == "\\.":
                                break
                        units[-1]["kind"] = "copyin"
                start = n
                break
            j += 1
        if start < n:
            buf.append(line[start:])
    flush()
    return units


# ---------------------------------------------------------------- psql variables and COPY rewriting

def _eval_set(args: str, vars_: dict[str, str]) -> str:
    out = ""
    for tok in re.findall(r":'(\w+)'|:(\w+)|'((?:[^']|'')*)'|(\S+)", args):
        v1, v2, lit, bare = tok
        if v1 or v2:
            out += vars_.get(v1 or v2, "")
        elif lit or tok[2] == "":
            out += lit.replace("''", "'")
        else:
            out += bare
    return out


def prepare_units(test: str) -> list[dict]:
    """Split a test file and rewrite server-side file COPY into client-side \\copy. Marks excluded units."""
    text = (REGRESS / "sql" / f"{test}.sql").read_text()
    units = split_units(text)
    vars_ = {"abs_srcdir": CLIENT_REGRESS, "abs_builddir": CLIENT_BUILD, "libdir": "/nonexistent", "dlsuffix": ".so"}
    for u in units:
        t = u["text"]
        if u["kind"] == "meta":
            m = re.match(r"\\set\s+(\w+)\s*(.*)$", t.strip())
            if m:
                vars_[m.group(1)] = _eval_set(m.group(2), vars_)
            if re.match(r"\\set\s+(VERBOSITY|SHOW_CONTEXT)\b", t.strip()):
                u["text"] = "\\echo\n"
                u["kind"] = "dropped"
            continue
        body = re.sub(r"^(\s*(--[^\n]*\n|/\*[\s\S]*?\*/))*\s*", "", t)
        m = re.match(r"^copy\s+([\s\S]+?)\s+(from|to)\s+:'(\w+)'([\s\S]*?);\s*$", body, re.I)
        if m and m.group(3) in vars_:
            path = vars_[m.group(3)]
            what = " ".join(m.group(1).split())
            rest = " ".join(m.group(4).split())
            u["text"] = f"\\copy {what} {m.group(2)} '{path}' {rest}\n"
            u["rewritten"] = True
            continue
        for pat, why in EXCLUDE_UNIT_PATTERNS:
            if pat.search(body if pat.pattern.startswith("^") else t):
                u["excluded"] = why
                break
    return units


# ---------------------------------------------------------------- output normalization

ERRLINE = re.compile(r"^(?:psql:[^:\n]*:\d+: )?(ERROR|FATAL|PANIC):  ")
MSGLINE = re.compile(r"^(?:psql:[^:\n]*:\d+: )?(NOTICE|WARNING|INFO|DEBUG|LOG):  ")
PSQLPFX = re.compile(r"^psql:[^:\n]*:\d+: ")


def normalize(lines: list[str]) -> list[str]:
    out = []
    skip_caret = False
    for ln in lines:
        if ERRLINE.match(ln):
            continue
        if re.match(r"^LINE \d+: ", ln):
            skip_caret = True
            continue
        if skip_caret and re.match(r"^\s*\^\s*$", ln):
            skip_caret = False
            continue
        skip_caret = False
        out.append(PSQLPFX.sub("psql: ", ln))
    return out


def sort_tables(lines: list[str]) -> list[str]:
    """Sort the body rows of every aligned result table (for statements whose row order is undefined)."""
    out, i = [], 0
    while i < len(lines):
        out.append(lines[i])
        if i + 1 < len(lines) and re.match(r"^-+(\+-+)*$", lines[i + 1] or "") is not None:
            out.append(lines[i + 1])
            j = i + 2
            body = []
            while j < len(lines) and not re.match(r"^\(\d+ rows?\)$", lines[j]):
                body.append(lines[j])
                j += 1
            out += sorted(body)
            i = j
            continue
        i += 1
    return out


def top_level_order_by(sql: str) -> bool:
    depth, s = 0, re.sub(r"'(?:[^']|'')*'", "''", sql.lower())
    flat = []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        flat.append(ch if depth == 0 else " ")
    return re.search(r"\border\s+by\b", "".join(flat)) is not None


# ---------------------------------------------------------------- driving psql

class Psql:
    """One psql process inside the client container, fed unit by unit."""

    def __init__(self, cli, host: str, test: str):
        self.tag = uuid.uuid4().hex[:12]
        env = {"PGTZ": "PST8PDT", "PGDATESTYLE": "Postgres, MDY", "PGAPPNAME": f"pg_regress/{test}",
               "PG_ABS_SRCDIR": CLIENT_REGRESS, "PG_ABS_BUILDDIR": CLIENT_BUILD, "PG_LIBDIR": "/nonexistent", "PG_DLSUFFIX": ".so",
               "PGCONNECT_TIMEOUT": "10"}
        self.cli = cli
        self.p = cli.popen(["sh", "-c", f"mkdir -p {CLIENT_BUILD}/results && exec stdbuf -oL -eL psql -X -h {host} -U postgres "
                                        f"-d regression -v VERBOSITY=terse -v SHOW_CONTEXT=never -v pgw_run={self.tag} 2>&1"], env=env)
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for raw in iter(self.p.stdout.readline, b""):
            self.q.put(raw.decode("utf-8", errors="replace").rstrip("\n"))
        self.q.put(None)

    def send(self, text: str) -> bool:
        try:
            self.p.stdin.write(text.encode())
            self.p.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def read_until(self, last_idx: int, timeout: float) -> tuple[dict[int, tuple[str, list[str]]], str]:
        """Collect lines per marker until marker `last_idx`. Returns ({idx: (sqlstate, lines)}, status)."""
        got: dict[int, tuple[str, list[str]]] = {}
        cur: list[str] = []
        deadline = time.time() + timeout
        while True:
            try:
                ln = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                return got, "timeout"
            if ln is None:
                return got, "exited"
            pos = ln.find(MARK + " ")
            if pos >= 0:
                if pos > 0:  # output without a trailing newline (\echo -n, \qecho -n) runs into the marker
                    cur.append(ln[:pos])
                parts = ln[pos:].split()
                idx = int(parts[1])
                got[idx] = (parts[2] if len(parts) > 2 else "", cur)
                cur = []
                if idx >= last_idx:
                    return got, "ok"
                continue
            cur.append(ln)

    def kill(self) -> None:
        self.cli.run(["pkill", "-KILL", "-f", f"pgw_run={self.tag}"], 20)
        try:
            self.p.kill()
        except OSError:
            pass

    def finish(self, timeout: float = 10) -> list[str]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rest = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ln = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if ln is None:
                break
            rest.append(ln)
        self.kill()
        return rest


def groups(units: list[dict]) -> list[list[int]]:
    """Unit indexes sent together: a lone unit, or a whole \\if ... \\endif region (skipped branches print no marker)."""
    out, cur, depth = [], [], 0
    for k, u in enumerate(units):
        t = u["text"].strip()
        cur.append(k)
        if u["kind"] == "meta" and re.match(r"\\if\b", t):
            depth += 1
        elif u["kind"] == "meta" and re.match(r"\\endif\b", t):
            depth = max(0, depth - 1)
        if depth == 0:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def run_test(cli, box, test: str, deadline: float) -> dict:
    """Run one test file; returns {"units": [...], "out": {idx: [sqlstate, lines] | status}}."""
    units = prepare_units(test)
    res: dict[int, list] = {}
    gs = groups(units)
    g = 0
    restarts = 0
    while g < len(gs):
        if time.time() > deadline:
            for gg in gs[g:]:
                for k in gg:
                    res[k] = ["<budget>", []]
            break
        ps = Psql(cli, box.host, test)
        while g < len(gs):
            grp = gs[g]
            text = "".join(units[k]["text"] + f"\\echo {MARK} {k} :SQLSTATE\n" for k in grp)
            if not ps.send(text):
                status, got = "exited", {}
            else:
                got, status = ps.read_until(grp[-1], UNIT_TIMEOUT_S * min(len(grp), 6))
            for k, v in got.items():
                res[k] = [v[0], v[1]]
            if status == "ok":
                g += 1
                continue
            # timeout or psql exited before this group's last marker
            ended_by_quit = any(re.match(r"\\q(uit)?\b", units[k]["text"].strip()) for k in grp)
            missing = [k for k in grp if k not in res]
            if status == "timeout":
                for k in missing[:1]:
                    res[k] = ["<timeout>", []]
                for k in missing[1:]:
                    res[k] = ["<not run>", []]
                ps.kill()
            else:
                for k in missing:
                    res[k] = ["<quit>" if ended_by_quit else "<connection lost>", []]
                ps.finish(2)
            g += 1
            if ended_by_quit and status == "exited":
                for gg in gs[g:]:
                    for k in gg:
                        res[k] = ["<quit>", []]
                g = len(gs)
                break
            from dbbox import handshake
            if not box.alive() or not handshake(box.host, box.port, timeout=5):
                restarts += 1
                if restarts > 8 or not box.restart():
                    for gg in gs[g:]:
                        for k in gg:
                            res[k] = ["<server down>", []]
                    g = len(gs)
            break
        else:
            ps.finish()
    return {"units": units, "out": {k: res.get(k, ["<missing>", []]) for k in range(len(units))}, "restarts": restarts}


def run_schedule(box, cli, tests: list[str] | None = None, budget_s: float = TOTAL_BUDGET_S, log=None) -> dict:
    deadline = time.time() + budget_s
    out = {}
    for t in tests or [t for t in schedule() if t not in EXCLUDED_TESTS]:
        t0 = time.time()
        r = run_test(cli, box, t, deadline)
        out[t] = {"out": r["out"], "restarts": r["restarts"], "seconds": round(time.time() - t0, 1)}
        if log:
            log(f"{t}: {len(r['units'])} units, {out[t]['seconds']} s, restarts {r['restarts']}")
    return out


# ---------------------------------------------------------------- reference and scoring

def unit_repr(state: str, lines: list[str], sql: str, kind: str) -> dict:
    lines = normalize(lines)
    err = state[:2] if re.fullmatch(r"[0-9A-Z]{5}", state or "") and state != "00000" else ""
    if state.startswith("<"):
        return {"status": state}
    unordered = kind == "sql" and not top_level_order_by(sql)
    return {"err": err if kind != "meta" else "", "lines": lines, "unordered": unordered}


def observable(rep: dict) -> bool:
    if "status" in rep:
        return False
    if rep["err"]:
        return True
    lines = [ln for ln in rep["lines"] if ln.strip()]
    if not lines:
        return False
    if len(lines) == 1 and re.fullmatch(r"[A-Z][A-Z ]*[A-Z]", lines[0]):  # bare command tag: CREATE TABLE, SET, BEGIN...
        return False
    return True


def same(exp: dict, got: dict) -> bool:
    if "status" in got or got.get("err") != exp.get("err"):
        return False
    if got["lines"] == exp["lines"]:
        return True
    return exp.get("unordered", False) and sort_tables(got["lines"]) == sort_tables(exp["lines"])


def build_expected(run_a: dict, run_b: dict) -> dict:
    """{test: [{"i", "sql", "exp"} for scored units], plus exclusion counts}."""
    expected, stats = {}, {"units": 0, "scored": 0, "excluded": {}, "nondeterministic": 0, "unobservable": 0}
    for test, ra in run_a.items():
        rb = run_b.get(test)
        if rb is None:
            continue
        units = prepare_units(test)
        items = []
        for k, u in enumerate(units):
            stats["units"] += 1
            if u.get("excluded") or u["kind"] == "dropped":
                why = u.get("excluded", "verbosity setting")
                stats["excluded"][why] = stats["excluded"].get(why, 0) + 1
                continue
            ea = unit_repr(*ra["out"][str(k)] if str(k) in ra["out"] else ra["out"][k], u["text"], u["kind"])
            eb = unit_repr(*rb["out"][str(k)] if str(k) in rb["out"] else rb["out"][k], u["text"], u["kind"])
            if ea != eb:
                stats["nondeterministic"] += 1
                continue
            if any("could not access file" in ln or "could not open file" in ln for ln in ea.get("lines", [])):
                stats["excluded"]["server filesystem"] = stats["excluded"].get("server filesystem", 0) + 1
                continue
            if not observable(ea):
                stats["unobservable"] += 1
                items.append({"i": k, "exp": ea, "scored": False})
                continue
            stats["scored"] += 1
            items.append({"i": k, "exp": ea, "scored": True})
        expected[test] = items
    return {"tests": expected, "stats": stats}


def score(run: dict, expected: dict) -> dict:
    per = {}
    for test, items in expected["tests"].items():
        scored = [it for it in items if it["scored"]]
        if not scored:
            continue
        units = prepare_units(test)
        got_run = run.get(test, {}).get("out", {})
        passed, last_ok, fails = 0, True, []
        for it in items:
            if not it["scored"]:
                continue
            k = it["i"]
            raw = got_run.get(str(k), got_run.get(k))
            got = unit_repr(*raw, units[k]["text"], units[k]["kind"]) if raw else {"status": "<missing>"}
            ok = same(it["exp"], got)
            if it["exp"]["err"]:
                ok = ok and last_ok
            else:
                last_ok = ok
            passed += ok
            if not ok and len(fails) < 8:
                fails.append({"i": k, "sql": units[k]["text"][:200], "exp": it["exp"], "got": got})
        per[test] = {"passed": passed, "total": len(scored), "fails": fails}
    s = sum(v["passed"] / v["total"] for v in per.values()) / max(1, len(per))
    return {"score": s, "files": len(per), "statements": f"{sum(v['passed'] for v in per.values())}/{sum(v['total'] for v in per.values())}",
            "per_file": {k: [v["passed"], v["total"]] for k, v in per.items()}, "fails": {k: v["fails"] for k, v in per.items() if v["fails"]}}


if __name__ == "__main__":
    import sys

    if sys.argv[1] == "units":
        for k, u in enumerate(prepare_units(sys.argv[2])):
            print(k, u["kind"], u.get("excluded", ""), repr(u["text"][:160]))
    elif sys.argv[1] == "count":
        tot = 0
        for t in schedule():
            if t in EXCLUDED_TESTS:
                continue
            n = len(prepare_units(t))
            tot += n
        print(tot, "units in", len([t for t in schedule() if t not in EXCLUDED_TESTS]), "files")
    print(json.dumps({}))
