import json, sys, time
sys.path.insert(0, "grader")
from dbbox import PgBox
import conformance as cf
b = PgBox(tag="try1")
try:
    t0 = time.time()
    print("start", b.start(), round(time.time() - t0, 1), flush=True)
    for n in cf.TESTS:
        if len(sys.argv) > 1 and sys.argv[1] not in n: continue
        t0 = time.time()
        r = cf.run_test(n, b.host, b.port)
        print(f"== {n} ({time.time()-t0:.1f}s)", json.dumps(r, ensure_ascii=False)[:1500], flush=True)
finally:
    b.close()
