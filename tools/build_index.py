"""Builds the home page (three tracks x top 3, then an archive) and the track pages /robotics/, /evals/, /post-training/.
Edit TRACKS / DATES below when posts change, then run: python3 tools/build_index.py"""
import html, os, re
from datetime import date
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

TRACKS = [  # (slug, name, one-line description, featured top 3 in order, rest)
    ("robotics", "Robotics", "Robot learning in production: policies, data collection, and a fleet at a customer site.",
     ["yam-track-env", "pi-api-beta", "pi0-zero-robot-data"],
     ["realtime-action-chunking", "led-tracking-accuracy", "eight-robots", "block-stacking-benchmark", "terrain-bench",
      "real2sim-case-packing-cell", "dsrl-libero", "remote-toy-cleanup"]),
    ("evals", "Agent evals", "Benchmarks and RL environments for coding agents, several of them controlling robots or physics simulations.",
     ["block-stacking-benchmark", "find-duplicates", "test-speedup-env"],
     ["yam-track-env", "terrain-bench", "cad-finger-benchmark", "trolley-bench", "real2sim-case-packing-cell"]),
    ("post-training", "Post-training", "Small SFT and RL experiments on Gemma 3 1B, each on one accelerator.",
     ["gemma-self-correction", "gemma-letters", "gemma-puzzles"],
     ["gemma-wordle"]),
]
DATES = {"trolley-bench": "2026-11-13", "pi-api-beta": "2026-10-13", "real2sim-case-packing-cell": "2026-10-13",
         "block-stacking-benchmark": "2026-11-10", "cad-finger-benchmark": "2026-10-13", "dsrl-libero": "2025-10-10",
         "eight-robots": "2026-11-11", "find-duplicates": "2026-11-15", "led-tracking-accuracy": "2026-01-23",
         "pi0-zero-robot-data": "2025-12-12", "realtime-action-chunking": "2026-06-28", "remote-toy-cleanup": "2024-03-19",
         "terrain-bench": "2026-11-13", "test-speedup-env": "2026-10-12", "yam-track-env": "2026-11-14",
         "gemma-letters": "2026-11-15", "gemma-puzzles": "2026-11-15", "gemma-self-correction": "2026-11-16", "gemma-wordle": "2026-11-16"}
FEATURED_ORDER = ["robotics", "evals", "post-training"]
# Home page and track pages are sorted best -> worst by this list (user's ranking, 2026-11-16).
RANKING = ["eight-robots", "gemma-wordle", "block-stacking-benchmark", "gemma-self-correction",
           "pi0-zero-robot-data", "gemma-letters", "terrain-bench",
           "led-tracking-accuracy", "yam-track-env", "gemma-puzzles",
           "pi-api-beta", "cad-finger-benchmark", "realtime-action-chunking", "dsrl-libero",
           "test-speedup-env", "remote-toy-cleanup", "find-duplicates", "real2sim-case-packing-cell",
           "trolley-bench"]

def meta(slug):
    s = open(f"posts/{slug}/index.html").read()
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", s, re.S)
    title = html.unescape(re.sub(r"<[^>]+>", "", h1.group(1))).strip() if h1 else html.unescape(re.search(r"<title>([^<]*)", s).group(1))
    d = re.search(r'<meta name="description" content="([^"]*)"', s)
    return title, html.unescape(d.group(1)) if d else ""

def fmt(dt):
    d = date.fromisoformat(dt); return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")

def tracks_of(slug):
    return [t for t in TRACKS if slug in t[3] or slug in t[4]]

def item(slug, pre):
    title, desc = meta(slug); dt = DATES[slug]; base = f"posts/{slug}"
    if os.path.exists(f"{base}/thumb.mp4"):
        th = f'<video src="{pre}{base}/thumb.mp4" poster="{pre}{base}/thumb.jpg" muted autoplay loop playsinline preload="metadata"></video>'
    elif os.path.exists(f"{base}/thumb.jpg"):
        th = f'<img src="{pre}{base}/thumb.jpg" alt="">'
    else:
        th = f'<span class="ph">{html.escape(title)}</span>'
    tags = "".join(f'<a href="{pre}{t[0]}/">{t[1].lower()}</a>' for t in tracks_of(slug))
    return f'''      <li>
        <a class="thumb" href="{pre}{base}/" tabindex="-1" aria-hidden="true">{th}</a>
        <time datetime="{dt}">{fmt(dt)}</time>
        <a href="{pre}{base}/">{html.escape(title)}</a>
        <p>{html.escape(desc)}</p>
        <div class="tags">{tags}</div>
      </li>'''

home = open("index.html").read()
HEAD = home[:home.index("<main>")]
HEAD = HEAD.replace("li.hide{display:none}\n", "").replace(".filter{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}\n", "").replace(".filter a.on{color:var(--bg);background:var(--ink);border-color:var(--ink)}\n", "")
if ".more{" not in HEAD:
    HEAD = HEAD.replace("</style>", ".more{display:inline-block;margin-top:12px;font:500 14px/1.4 var(--mono);color:var(--ink);text-decoration:underline;text-decoration-color:var(--accent);text-underline-offset:3px}\n.more:hover{color:var(--accent)}\nsection>p.lede{margin:0 0 12px;color:var(--muted);max-width:640px}\nli .thumb .ph{display:flex;align-items:center;justify-content:center;height:100%;padding:12px;text-align:center;font:700 18px/1.2 var(--display);color:var(--muted)}\n.back{font:500 13px/1 var(--mono);color:var(--muted);text-decoration:none}.back:hover{color:var(--accent)}\n</style>")
LINKS = re.search(r'<nav class="links">.*?</nav>', home, re.S).group(0)

def page(title, pre, header_extra, sections):
    links = LINKS.replace('href="Jack_Dubie_Resume.pdf"', f'href="{pre}Jack_Dubie_Resume.pdf"')
    head = re.sub(r"<title>[^<]*</title>", f"<title>{html.escape(title)}</title>", HEAD)
    return head + f'''<main>
  <header class="top">
{header_extra}    {links}
  </header>
{sections}
</main>
</html>
'''

# ---- home: one list, best -> worst, with links to the track pages under the name
all_posts = {x for t in TRACKS for x in t[3] + t[4]}
assert set(RANKING) == all_posts, (all_posts - set(RANKING), set(RANKING) - all_posts)
trackrow = '    <nav class="links trackrow">' + '<span>·</span>'.join(f'<a href="{t[0]}/">{t[1]}</a>' for t in TRACKS) + '</nav>\n'
secs = f'''  <section>
    <ul>
{chr(10).join(item(x, "") for x in RANKING)}
    </ul>
  </section>'''
open("index.html", "w").write(page("Jack Dubie", "", "    <h1>Jack Dubie</h1>\n" + trackrow, secs))

# ---- track pages
for key, name, desc, feat, others in TRACKS:
    ranked = sorted(feat + others, key=RANKING.index)
    extra = ""
    if key == "post-training":
        extra = '\n    <p class="lede">Also: the <a href="../posts/gemma-1b/">Teaching Gemma 1B</a> series index.</p>'
    secs = f'''  <section>
    <p class="lede">{html.escape(desc)}</p>{extra}
    <ul>
{chr(10).join(item(s, "../") for s in ranked)}
    </ul>
  </section>'''
    os.makedirs(key, exist_ok=True)
    open(f"{key}/index.html", "w").write(page(f"{name} · Jack Dubie", "../", f'    <a class="back" href="../">← Jack Dubie</a>\n    <h1>{name}</h1>\n', secs))
all_posts = {s for t in TRACKS for s in t[3] + t[4]}
missing = [d for d in os.listdir("posts") if os.path.exists(f"posts/{d}/index.html") and d not in all_posts and d != "gemma-1b"]
print("posts not in any track:", missing)
