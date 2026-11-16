"""Builds the home page (three tracks x top 3, then an archive) and the track pages /robotics/, /evals/, /post-training/.
Edit TRACKS / DATES below when posts change, then run: python3 tools/build_index.py"""
import html, os, re
from datetime import date
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

LABELS = [  # (slug used in ?tag=, display name), in display order
    ("robot-data-collection", "Robot data collection"),
    ("robot-policy-training", "Robot policy training"),
    ("robot-deployment", "Robot deployment"),
    ("llm-post-training", "LLM post-training"),
    ("agent-evals", "Agent evals"),
]
POST_LABELS = {
    "led-tracking-accuracy": ["robot-data-collection"],
    "pi0-zero-robot-data": ["robot-data-collection", "robot-policy-training"],
    "remote-toy-cleanup": ["robot-data-collection"],
    "realtime-action-chunking": ["robot-policy-training"],
    "dsrl-libero": ["robot-policy-training"],
    "eight-robots": ["robot-deployment"],
    "pi-api-beta": ["robot-deployment"],
    "real2sim-case-packing-cell": ["robot-deployment", "agent-evals"],
    "gemma-wordle": ["llm-post-training"], "gemma-self-correction": ["llm-post-training"],
    "gemma-letters": ["llm-post-training"], "gemma-puzzles": ["llm-post-training"],
    "block-stacking-benchmark": ["agent-evals"], "yam-track-env": ["agent-evals"], "terrain-bench": ["agent-evals"],
    "cad-finger-benchmark": ["agent-evals"], "test-speedup-env": ["agent-evals"], "find-duplicates": ["agent-evals"],
    "trolley-bench": ["agent-evals"],
}
DATES = {"trolley-bench": "2026-11-13", "pi-api-beta": "2026-10-13", "real2sim-case-packing-cell": "2026-10-13",
         "block-stacking-benchmark": "2026-11-10", "cad-finger-benchmark": "2026-10-13", "dsrl-libero": "2025-10-10",
         "eight-robots": "2026-11-11", "find-duplicates": "2026-11-15", "led-tracking-accuracy": "2026-01-23",
         "pi0-zero-robot-data": "2025-12-12", "realtime-action-chunking": "2026-06-28", "remote-toy-cleanup": "2024-03-19",
         "terrain-bench": "2026-11-13", "test-speedup-env": "2026-10-12", "yam-track-env": "2026-11-14",
         "gemma-letters": "2026-11-15", "gemma-puzzles": "2026-11-15", "gemma-self-correction": "2026-11-16", "gemma-wordle": "2026-11-16"}
FEATURED_ORDER = ["robotics", "evals", "post-training"]
# Home page and track pages are sorted best -> worst by this list (user's ranking, 2026-11-16).
RANKING = ["eight-robots", "gemma-wordle", "block-stacking-benchmark", "gemma-puzzles",
           "pi0-zero-robot-data", "gemma-letters", "terrain-bench",
           "led-tracking-accuracy", "yam-track-env", "gemma-self-correction",
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

NAME = dict(LABELS)

def item(slug, pre):
    title, desc = meta(slug); dt = DATES[slug]; base = f"posts/{slug}"
    if os.path.exists(f"{base}/thumb.mp4"):
        th = f'<video src="{pre}{base}/thumb.mp4" poster="{pre}{base}/thumb.jpg" muted autoplay loop playsinline preload="metadata"></video>'
    elif os.path.exists(f"{base}/thumb.jpg"):
        th = f'<img src="{pre}{base}/thumb.jpg" alt="">'
    else:
        th = f'<span class="ph">{html.escape(title)}</span>'
    tags = "".join(f'<a href="{pre}?tag={t}" data-tag="{t}">{NAME[t].lower()}</a>' for t in POST_LABELS[slug])
    return f'''      <li data-tags="{' '.join(POST_LABELS[slug])}">
        <a class="thumb" href="{pre}{base}/" tabindex="-1" aria-hidden="true">{th}</a>
        <time datetime="{dt}">{fmt(dt)}</time>
        <a href="{pre}{base}/">{html.escape(title)}</a>
        <p>{html.escape(desc)}</p>
        <div class="tags">{tags}</div>
      </li>'''

home = open("index.html").read()
HEAD = home[:home.index("<main>")]
for rule in ("li.hide{display:none}", ".filter{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}", ".filter a.on{color:var(--bg);background:var(--ink);border-color:var(--ink)}"):
    if rule not in HEAD: HEAD = HEAD.replace("</style>", rule + "\n</style>")
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

# ---- home: one list, best -> worst; label buttons filter it via ?tag=<label> (shareable)
assert set(RANKING) == set(POST_LABELS), (set(POST_LABELS) ^ set(RANKING))
for v in POST_LABELS.values(): assert all(t in NAME for t in v)
filt = '    <nav class="filter" aria-label="Filter posts"><a href="./" data-tag="">All</a>' + "".join(
    f'<a href="?tag={t}" data-tag="{t}">{n}</a>' for t, n in LABELS) + "</nav>\n"
script = """<script>
(function(){
  var legacy={"#agent-evals":"agent-evals","#policy-learning":"robot-policy-training","#data-collection":"robot-data-collection","#deployment":"robot-deployment","#reproduction":"robot-policy-training"};
  function current(){var t=new URLSearchParams(location.search).get("tag")||"";return t;}
  function apply(t){
    document.querySelectorAll("ul li").forEach(function(li){
      var tags=(li.getAttribute("data-tags")||"").split(" ");
      li.classList.toggle("hide", !!t && tags.indexOf(t)<0);});
    document.querySelectorAll(".filter a").forEach(function(a){a.classList.toggle("on",a.getAttribute("data-tag")===t);});
  }
  function go(t){var u=new URL(location.href);if(t)u.searchParams.set("tag",t);else u.searchParams.delete("tag");u.hash="";history.pushState(null,"",u);apply(t);}
  document.addEventListener("click",function(e){var a=e.target.closest("a[data-tag]");if(!a)return;e.preventDefault();go(a.getAttribute("data-tag"));window.scrollTo({top:0});});
  window.addEventListener("popstate",function(){apply(current());});
  window.addEventListener("hashchange",function(){if(legacy[location.hash])go(legacy[location.hash]);});
  if(!current()&&legacy[location.hash]){go(legacy[location.hash]);}else{apply(current());}
})();
</script>
"""
secs = f"""  <section>
    <ul>
{chr(10).join(item(x, "") for x in RANKING)}
    </ul>
  </section>"""
html_out = page("Jack Dubie", "", "    <h1>Jack Dubie</h1>\n", secs).replace("  </header>", filt + "  </header>", 1).replace("</main>\n</html>", "</main>\n" + script + "</html>")
open("index.html", "w").write(html_out)

# old track pages -> redirects to the filtered home page
def redirect(to):
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="robots" content="noindex">
<title>Moved</title>
<link rel="canonical" href="{to}">
<meta http-equiv="refresh" content="0; url={to}">
<p>Moved to <a href="{to}">{to}</a>.</p>
</html>
"""
for d, to in (("robotics", "/"), ("evals", "/?tag=agent-evals"), ("post-training", "/?tag=llm-post-training")):
    os.makedirs(d, exist_ok=True); open(f"{d}/index.html", "w").write(redirect(to))
print("ok", len(RANKING), "posts")
