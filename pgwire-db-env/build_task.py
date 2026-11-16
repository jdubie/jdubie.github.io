"""Assemble the agent's public kit in task/ from the grader's own code and PostgreSQL's recorded answers.

  python3 build_task.py

Writes (all regenerable; task/tools/pgproto.py and pgwire.py are the hand-written originals):
  task/tools/dbbox.py, run_public.py      the local shim and runner (from task/kit/)
  task/tools/conformance_public.py         grader/conformance.py with only the public tests left in
  task/tools/clients_suite.py, clients/    grader/clients.py and its scripts, plus expected.json
  task/tools/regress.py, acid.py, scale.py the grader's harnesses, pointed at /work paths
  task/public/regress/                     upstream src/test/regress: sql/, data/, parallel_schedule
  task/public/expected/                    conformance.json (public subset); regress.json comes from
                                           `make_reference.py public_regress`
  docs-build/docs/{html,txt}               the PostgreSQL 16.16 manual (the HTML shipped in the source
                                           tarball, plus a plain-text copy); run_agents.py mounts it
                                           read-only at /work/docs (v2.1). Manual only: no source, no
                                           regress expected output.
Never copies hidden expected answers, crashpoint code or the reference servers.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tempfile
from html.parser import HTMLParser
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
G = ROOT / "grader"
TOOLS = ROOT / "task/tools"
PUB = ROOT / "task/public"
SRC_REGRESS = ROOT / "pgsrc/postgresql-16.16/src/test/regress"

PUBLIC_PROTOCOL = [
    "startup_trust", "ssl_request_refused", "scram_ok", "md5_ok", "cleartext_ok", "unknown_role", "simple_types_text",
    "simple_multi_statement", "simple_empty_query", "simple_error_stops_rest", "txn_status_bytes", "command_tags",
    "notice_response", "ext_unnamed_basic", "ext_describe_statement", "ext_named_statement_reuse", "ext_portal_row_limit",
    "ext_error_skips_to_sync", "ext_pipeline_implicit_txn", "bin_results_all", "bin_params", "copy_from_text",
    "copy_to_text_csv_binary", "cancel_request", "notify_other_session",
]


def strip_tests(src: str, keep: set[str]) -> str:
    """Remove every top-level `def t_<name>` whose name is not public (helpers stay)."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    drop = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("t_") and node.name[2:] not in keep:
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            drop.append((start, node.end_lineno))
    for a, b in reversed(drop):
        del lines[a:b]
    out = "".join(lines)
    ast.parse(out)
    return out


class _Text(HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "dt", "dd", "pre", "table", "title"}

    def __init__(self):
        super().__init__()
        self.out, self.skip, self.pre = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "pre":
            self.pre += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "td":
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip -= 1
        if tag == "pre":
            self.pre -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        t = "".join(self.out)
        return re.sub(r"\n[ \t]*\n(\s*\n)+", "\n\n", t).strip() + "\n"


def build_docs() -> None:
    """The manual from the release tarball (it ships built HTML), plus plain text for grep."""
    out = ROOT / "docs-build/docs"
    if out.exists():
        for p in [out, *out.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        shutil.rmtree(out)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["tar", "xjf", str(ROOT / "pgsrc/pg16.tar.bz2"), "-C", tmp, "postgresql-16.16/doc/src/sgml/html"], check=True)
        shutil.copytree(Path(tmp) / "postgresql-16.16/doc/src/sgml/html", out / "html")
    (out / "txt").mkdir()
    for h in sorted((out / "html").glob("*.html")):
        p = _Text()
        p.feed(h.read_text(errors="replace"))
        (out / "txt" / (h.stem + ".txt")).write_text(p.text())
    (out / "README.txt").write_text(
        "The PostgreSQL 16.16 manual, read-only. html/ is the HTML shipped in the release tarball (open index.html);\n"
        "txt/ is the same pages as plain text, one file per page, for grep. Start with: protocol.txt and\n"
        "protocol-message-formats.txt, sasl-authentication.txt, protocol-flow.txt (COPY, cancel), errcodes-appendix.txt,\n"
        "sql-commands.txt (then sql-<command>.txt), datatype*.txt, functions*.txt.\n")
    for p in out.rglob("*"):
        p.chmod(0o555 if p.is_dir() else 0o444)
    out.chmod(0o555)


def main() -> None:
    TOOLS.mkdir(parents=True, exist_ok=True)
    for f in ("dbbox.py", "run_public.py"):
        shutil.copy(ROOT / "task/kit" / f, TOOLS / f)

    exp = json.loads((G / "expected/conformance.json").read_text())["tests"]
    missing = [n for n in PUBLIC_PROTOCOL if n not in exp]
    assert not missing, f"public protocol tests without a reference: {missing}"
    src = strip_tests((G / "conformance.py").read_text(), set(PUBLIC_PROTOCOL))
    src = src.replace("python3 grader/conformance.py", "python3 tools/conformance_public.py")
    (TOOLS / "conformance_public.py").write_text(src)
    (PUB / "expected").mkdir(parents=True, exist_ok=True)
    (PUB / "expected/conformance.json").write_text(json.dumps({"tests": {n: exp[n] for n in PUBLIC_PROTOCOL}}, indent=0,
                                                              ensure_ascii=False))

    cdir = TOOLS / "clients"
    if cdir.exists():
        shutil.rmtree(cdir)
    shutil.copytree(G / "clients", cdir)
    for p in cdir.iterdir():
        if p.suffix in (".sql", ".py", ".js", ".java"):
            p.write_text(p.read_text().replace("'/clients/", "'/work/tools/clients/"))
    shutil.copy(G / "expected/clients.json", cdir / "expected.json")
    (TOOLS / "clients_suite.py").write_text((G / "clients.py").read_text().replace('"/clients/', '"/work/tools/clients/'))

    rg = (G / "regress.py").read_text()
    rg = rg.replace('REGRESS = ROOT / "pgsrc/postgresql-16.16/src/test/regress"', 'REGRESS = ROOT / "public/regress"')
    rg = rg.replace('CLIENT_REGRESS = "/regress"', 'CLIENT_REGRESS = "/work/public/regress"')
    assert 'ROOT / "public/regress"' in rg and '"/work/public/regress"' in rg
    (TOOLS / "regress.py").write_text(rg)
    for f in ("acid.py", "scale.py"):
        shutil.copy(G / f, TOOLS / f)

    rdir = PUB / "regress"
    if rdir.exists():
        shutil.rmtree(rdir)
    rdir.mkdir(parents=True)
    shutil.copytree(SRC_REGRESS / "sql", rdir / "sql")
    shutil.copytree(SRC_REGRESS / "data", rdir / "data")
    shutil.copy(SRC_REGRESS / "parallel_schedule", rdir / "parallel_schedule")

    if not (PUB / "expected/regress.json").exists():
        print("WARNING: task/public/expected/regress.json missing; run `python3 make_reference.py public_regress`")
    build_docs()
    print("docs:", sum(1 for _ in (ROOT / "docs-build/docs/txt").glob("*.txt")), "pages")
    print("kit built:", sorted(p.name for p in TOOLS.iterdir() if p.name != "__pycache__"))


if __name__ == "__main__":
    main()
