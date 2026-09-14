#!/usr/bin/env python3
"""Contribution Quest generator.

Fetches every merged PR authored by SIDDHANTCOOKIE via the GitHub search API,
classifies each into one of the four quest paths, and rewrites the
contribution-quest section of README.md between the markers:
  <!-- contribution-quest:begin --> ... <!-- contribution-quest:end -->

Runs hourly via .github/workflows/contribution-quest.yml (cron) or manually
via workflow_dispatch. GITHUB_TOKEN env var is used when present (Actions);
without it the unauthenticated rate limit still covers the 1-2 search calls.
"""
import json, os, re, sys, time, urllib.request, urllib.error

USER = "SIDDHANTCOOKIE"
BEGIN = "<!-- contribution-quest:begin -->"
END = "<!-- contribution-quest:end -->"

# --- classification -------------------------------------------------------
# Kinds of work:
#   mind   - ai & products that think (his own repos + research work)
#   blade  - security & hardening, anywhere (keyword match on title)
#   chain  - web3 & protocol engineering (StabilityNexus, healthyinc)
#   scroll - products & community (formstr suite, aossie, anything else)
SECURITY_KW = re.compile(
    r"securit|reentranc|vulnerab|exploit|audit|signature|sniping|overflow|spoof|forge",
    re.IGNORECASE,
)
# hand-verified exceptions the keywords miss (repo, pr#) -> path
OVERRIDES = {
    ("healthyinc/bio-block", 106): "blade",  # call-vs-transfer reentrancy-pattern fix
}

def classify(repo, number, title):
    if (repo, number) in OVERRIDES:
        return OVERRIDES[(repo, number)]
    if SECURITY_KW.search(title):
        return "blade"
    org = repo.split("/")[0]
    if org in ("StabilityNexus", "healthyinc"):
        return "chain"
    if org in ("SIDDHANTCOOKIE", "DemocratiseResearch"):
        return "mind"
    if org in ("formstr-hq", "AOSSIE-Org"):
        return "scroll"
    return "scroll"

# --- fetch ----------------------------------------------------------------
def http_json(url, token):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "contribution-quest-generator",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            raise

def fetch_merged_prs(token):
    prs, page = [], 1
    while True:
        q = f"author:{USER}+type:pr+is:merged"
        url = (f"https://api.github.com/search/issues?q={q}"
               f"&per_page=100&page={page}&sort=created&order=desc")
        data = http_json(url, token)
        items = data.get("items", [])
        for it in items:
            repo = it["repository_url"].split("https://api.github.com/repos/")[-1]
            merged = (it.get("pull_request") or {}).get("merged_at")
            if not merged:
                continue
            prs.append({
                "repo": repo,
                "number": it["number"],
                "title": it["title"].strip(),
                "body": (it.get("body") or "").strip(),
                "merged_at": merged[:10],
                "url": f"https://github.com/{repo}/pull/{it['number']}",
            })
        if len(items) < 100 or len(prs) >= data.get("total_count", 0):
            break
        page += 1
        time.sleep(2)
    prs.sort(key=lambda p: p["merged_at"], reverse=True)
    for p in prs:
        p["gist"] = gist(p, token)
    return prs

# --- render ---------------------------------------------------------------
MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"]
def fdate(iso):
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m)-1]} {int(d)}, {y}"

def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_pr_body(body):
    """Turn PR-authored prose into readable text without trusting the title."""
    body = re.sub(r"<!--.*?-->", " ", body or "", flags=re.DOTALL)
    body = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    body = re.sub(r"<img\b[^>]*>|https?://\S+", " ", body, flags=re.IGNORECASE)
    body = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", body)
    body = re.sub(r"(?m)^\s*[-*+]\s+", "", body)
    body = re.sub(r"(?m)^\s*\d+[.)]\s+", "", body)
    body = re.sub(r"\[[ xX]\]", " ", body)
    body = re.sub(r"[*_`~]", "", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body

SKIP_PHRASES = (
    "while ai can be", "submissions that do not meet", "my code follows",
    "requesting review", "reviewer:", "type: feature", "breaking change",
    "add screenshots", "screenshots/recordings", "additional notes",
    "code style and conventions", "applicable, i have", "summary by coderabbit",
    "fixes #(todo", "checkboxes:", "yes no", "current code on main",
    "please take the time", "always read through", "i have used the following ai",
    "contributions meet the task", "closed without warning", "checklist my pr",
    "ai slop is", "may lead to banning", "by coderabbit", "do not spam our repos",
)
ACTION = re.compile(
    r"\b(add(?:s|ed)?|fix(?:es|ed)?|implement(?:s|ed)?|replace(?:s|d)?|"
    r"remove(?:s|d)?|refactor(?:s|ed)?|restore(?:s|d)?|introduce(?:s|d)?|"
    r"migrate(?:s|d)?|prevent(?:s|ed)?|support(?:s|ed)?|update(?:s|d)?|"
    r"change(?:s|d)?|allow(?:s|ed)?|create(?:s|d)?|build(?:s|t)?|"
    r"resolve(?:s|d)?|ensure(?:s|d)?|cache(?:s|d)?|validate(?:s|d)?)\b",
    re.IGNORECASE,
)

def meaningful_body_chunks(body):
    text = clean_pr_body(body)
    chunks = re.split(r"(?<=[.!?])\s+|\s*[;•]\s*", text)
    ranked = []
    for index, chunk in enumerate(chunks):
        chunk = re.sub(r"^(?:this (?:pr|change)|the pr)\s+", "", chunk.strip(), flags=re.I)
        chunk = re.sub(r"^(?:summary|description|overview|what changed|solution)\s*:?\s*", "", chunk, flags=re.I)
        low = chunk.lower().strip(" :.-")
        if len(low) < 18 or low in ("n/a", "none"):
            continue
        if any(x in low for x in SKIP_PHRASES) or not re.search(r"[a-zA-Z]{3}", chunk):
            continue
        score = (4 if ACTION.search(chunk) else 0)
        score += (2 if 35 <= len(chunk) <= 240 else 0)
        score += (1 if index < 8 else 0)
        score -= (2 if chunk.count("@") > 1 else 0)
        ranked.append((score, -index, chunk.strip(" -:;")))
    ranked.sort(reverse=True)
    return [x[2] for x in ranked]

def fallback_gist(repo, number, token):
    """Use changed file paths and commit messages when the PR body has no substance."""
    base = f"https://api.github.com/repos/{repo}/pulls/{number}"
    files = http_json(base + "/files?per_page=100", token)
    commits = http_json(base + "/commits?per_page=100", token)
    paths = [f.get("filename", "") for f in files if f.get("filename")]
    messages = [c.get("commit", {}).get("message", "").split("\n", 1)[0]
                for c in commits]
    message_chunks = meaningful_body_chunks(". ".join(messages))
    if message_chunks:
        return message_chunks[0]
    if paths:
        shown = ", ".join(paths[:3])
        extra = f" and {len(paths)-3} more" if len(paths) > 3 else ""
        return f"Changed {shown}{extra}"
    return "Updated the repository; open the PR for the exact diff"

def gist(p, token):
    chunks = meaningful_body_chunks(p.get("body", ""))
    text = chunks[0] if chunks else fallback_gist(p["repo"], p["number"], token)
    text = text[0].upper() + text[1:] if text else text
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text if len(text) <= 150 else text[:149].rsplit(" ", 1)[0] + "…"

def pr_line(p):
    return (f'<sub><a href="{p["url"]}"><b>#{p["number"]}</b></a> '
            f'{esc(p["gist"])} · {fdate(p["merged_at"])}</sub><br>')

def repo_group(repo, prs):
    n = len(prs)
    lines = [f'<sub><b>{repo}</b> · {n} merged</sub><br>']
    lines += [pr_line(p) for p in prs]
    return "\n".join(lines)

INTRO = {
    "mind":   "skillcheck, paperly &amp; research",
    "blade":  "security work, merged upstream",
    "chain":  "web3 &amp; protocol engineering",
    "scroll": "formstr, aossie &amp; community",
}
KANJI = {"mind": "\u5fc3", "blade": "\u5203", "chain": "\u9396", "scroll": "\u5dfb"}
TAGLINE = {
    "mind": "ai that shows its work",
    "blade": "cutting vulnerabilities out",
    "chain": "a blockchain, built from scratch",
    "scroll": "the formstr suite, shipped",
}
ORDER = ["mind", "blade", "chain", "scroll"]

def path_block(path, prs):
    groups = {}
    for p in prs:
        groups.setdefault(p["repo"], []).append(p)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    body = "\n<br>\n".join(repo_group(r, ps) for r, ps in ordered)
    n = len(prs)
    return f"""<details name="xp">
<summary><b>{KANJI[path]} the way of the {path}</b> - {TAGLINE[path]} · <b>{n} merged prs</b></summary>
<a id="xp-{path}"></a>
<p align="center"><img src="assets/xp_{path}.gif" width="560"></p>
<p align="center"><b>{INTRO[path]}</b></p>
<p align="center">
{body}
<br>
<sub><a href="#user-content-xp-map">↩ return to the crossroads</a></sub>
</p>
</details>"""

def render_section(prs):
    by_path = {"mind": [], "blade": [], "chain": [], "scroll": []}
    for p in prs:
        by_path[classify(p["repo"], p["number"], p["title"])].append(p)
    total = len(prs)
    counts = {k: len(v) for k, v in by_path.items()}
    nav = " &nbsp;·&nbsp;\n".join(
        f'<a href="#user-content-xp-{p}"><b>{KANJI[p]} the way of the {p}</b> · {counts[p]}</a>'
        for p in ORDER) + "<br>"
    parts = [f"""## Contribution Quest

<details name="xp" open>
<summary><b>༄ the crossroads</b> - a contribution quest</summary>
<a id="xp-map"></a>

https://github.com/user-attachments/assets/ca34406c-c111-4bfc-992c-5886b480b1a3

<p align="center"><sub>▶ the way of the ronin soundtrack - press play, then unmute - GitHub blocks autoplay with sound</sub></p>
<p align="center">
<sub>four paths, four kinds of work - every merged pull request lives here.<br>
<b>{total} merged prs</b> and counting; the quest renews itself with each new merge.</sub><br><br>
{nav}
</p>
</details>""",
        *(path_block(p, by_path[p]) for p in ORDER),
    ]
    return "\n\n".join(parts), counts

def main():
    readme = sys.argv[1] if len(sys.argv) > 1 else "README.md"
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    prs = fetch_merged_prs(token)
    section, counts = render_section(prs)
    src = open(readme).read()
    if BEGIN not in src or END not in src:
        print("markers not found in", readme, file=sys.stderr)
        sys.exit(1)
    out = src.split(BEGIN)[0] + BEGIN + "\n\n" + section + "\n\n" + END + src.split(END)[1]
    if out != src:
        open(readme, "w").write(out)
        print("updated", readme)
    else:
        print("no change")
    print("counts:", json.dumps(counts), "total:", len(prs))

if __name__ == "__main__":
    main()
