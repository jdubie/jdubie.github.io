import json, sys, time
sys.path.insert(0, "grader")
from dbbox import PgBox, ClientBox
import clients as cl
cli = ClientBox(tag="tryc")
try:
    t0 = time.time()
    r = cl.run_all(lambda: PgBox(), cli)
    print(round(time.time() - t0), "s")
    json.dump(r, open("logs/try_clients.json", "w"), indent=1, ensure_ascii=False)
    for g in cl.GROUPS:
        print(g, json.dumps(r.get(g), ensure_ascii=False)[:2500])
finally:
    cli.close()
