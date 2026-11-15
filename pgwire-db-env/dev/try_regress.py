import json, sys, time
sys.path.insert(0, "grader")
from dbbox import PgBox, ClientBox
import regress as rg
tests = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] != "all" else None
out = sys.argv[2] if len(sys.argv) > 2 else "logs/try_regress.json"
cli = ClientBox(tag="tryr")
box = PgBox(tag="tryr")
try:
    assert box.start()
    t0 = time.time()
    r = rg.run_schedule(box, cli, tests, log=lambda m: print(m, flush=True))
    print("total", round(time.time() - t0), "s", flush=True)
    json.dump(r, open(out, "w"), ensure_ascii=False)
finally:
    box.close(); cli.close()
