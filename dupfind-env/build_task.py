"""Build the agent's task kit: public case generators (hidden-only cases stripped) + their expected answers."""

from __future__ import annotations

import ast
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASK = ROOT / "task"
sys.path.insert(0, str(ROOT / "grader"))
import cases  # noqa: E402
import grade  # noqa: E402
import reffind  # noqa: E402

DROP_ASSIGN = {"HIDDEN", "GATES", "HIDDEN_SEED"}


def public_generator() -> str:
    src = (ROOT / "grader/cases.py").read_text()
    hidden_only = set(cases.HIDDEN) - set(cases.PUBLIC) | {"crc32_twin", "_sudo", "_mount_points", "_remount_ro"}
    keep = []
    for node in ast.parse(src).body:
        name = getattr(node, "name", None)
        if name in hidden_only:
            continue
        targets = {n.id for t in getattr(node, "targets", []) for n in ast.walk(t) if isinstance(n, ast.Name)}
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
            continue  # module docstring
        if "PUBLIC_SCALE" in targets:
            keep.append(f"PUBLIC_SCALE, PUBLIC_SEED = {cases.PUBLIC_SCALE}, {cases.PUBLIC_SEED}\nHIDDEN = PUBLIC")
        elif not targets & DROP_ASSIGN:
            keep.append(ast.get_source_segment(src, node))
    head = ('"""Generators for the public cases, at public scale (the hidden ones are the same kind of thing, bigger,\n'
            'plus other cases). Usage: python3 tools/cases_public.py CASE OUT_DIR"""\n\n')
    tail = ('\n\n\nif __name__ == "__main__":\n    import sys\n    build(sys.argv[1], os.fsencode(sys.argv[2]), PUBLIC_SEED, PUBLIC_SCALE)\n')
    return head + "\n\n\n".join(keep).replace("import zlib\n", "") + tail


def main() -> None:
    (TASK / "tools").mkdir(parents=True, exist_ok=True)
    (TASK / "tools/cases_public.py").write_text(public_generator())
    exp = TASK / "public/expected"
    if exp.exists():
        shutil.rmtree(exp)
    exp.mkdir(parents=True)
    scratch = b"/dev/shm/dupfind-public-build"
    for name in cases.PUBLIC:
        root = scratch + b"/" + name.encode()
        cases.remove(root)
        cases.build(name, root, cases.PUBLIC_SEED, cases.PUBLIC_SCALE)
        groups = reffind.naive(root)
        grade.EXPECTED = exp
        grade.dump_expected(name, groups)
        cases.remove(root)
        print(f"{name:14s} {len(groups)} groups")
    os.rmdir(scratch)


if __name__ == "__main__":
    main()
