"""Write seed-free summaries of graded runs into results/<name>/ (committed); raw runs stay in runs/ (ignored).

  python3 export_results.py <run_dir> <results_name>
"""

import json
import re
import sys
from pathlib import Path

run, name = Path(sys.argv[1]), sys.argv[2]
out = Path(__file__).resolve().parent / "results" / name
out.mkdir(parents=True, exist_ok=True)
tl = []
for p in sorted((run / "grades").glob("*/grade.json")):
    g = json.loads(p.read_text())
    m = re.match(r"t(\d+)min", p.parent.name)
    meta = json.loads((run / "meta.json").read_text())
    tl.append({"snapshot": p.parent.name, "t_min": int(m.group(1)) if m else round(meta["seconds"] / 60, 1), **g["summary"],
               "oracle_version": g["meta"].get("oracle_version", 1)})
(out / "timeline.json").write_text(json.dumps(tl, indent=1))
meta = json.loads((run / "meta.json").read_text())
meta["seed"] = meta["seed"] if meta.get("seed") == 7101 else "private"
(out / "meta.json").write_text(json.dumps(meta, indent=1))
print(name, [(x["snapshot"], x["reward"]) for x in tl if x["snapshot"] == "final"])
