"""Rescore existing grades under both scorings without rerunning anything (per-workload scores are stored).

  python3 rescore.py <label>=<grade.json> ...
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "grader"))
from grade import EXACT_PARTIAL, WEIGHTS, summarize_workloads  # noqa: E402

for arg in sys.argv[1:]:
    label, path = arg.split("=", 1)
    g = json.loads(Path(path).read_text())
    w = summarize_workloads(g["per_workload"])
    dur = g["durability"]["score"]
    part = WEIGHTS["sql"] * w["sql"]["score_partial"] + WEIGHTS["protocol"] * w["protocol"]["score_partial"] + WEIGHTS["durability"] * dur
    exact = WEIGHTS["sql"] * w["sql"]["score_exact"] + WEIGHTS["protocol"] * w["protocol"]["score_exact"] + WEIGHTS["durability"] * dur
    print(json.dumps({"label": label, "reward_partial": round(part, 4), "reward_exact": round(exact, 4),
                      "sql_partial": round(w["sql"]["score_partial"], 4), "sql_exact": round(w["sql"]["score_exact"], 4),
                      "protocol_partial": round(w["protocol"]["score_partial"], 4), "protocol_exact": round(w["protocol"]["score_exact"], 4),
                      "durability": dur, "exact_sessions": f"{w['sql']['perfect']}/{w['sql']['n']}",
                      "exact_protocol": f"{w['protocol']['perfect']}/{w['protocol']['n']}", "exact_partial_factor": EXACT_PARTIAL}))
