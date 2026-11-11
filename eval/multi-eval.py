#!/usr/bin/env python3
"""All-required@k over a multi-page gold set (a probe of the acceptance gate since 2026-11-11).

Each item is {"q": ..., "required": [page, page, ...]}: an answer needs every
listed page, unlike run-eval.py's `gold`, where any one page counts (after
obsidian-second-brain's `requiredPages` / `allRequired@k`, v3.6.0). The vault
is WIKI_VAULT (default ~/projects/knowledge-vault); the gold path is relative
to its eval/ unless absolute.

  python3 eval/multi-eval.py goldset-multi.json [TOP] [PER_PAGE_CAP]
"""
import json, os, subprocess, sys
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
ENGINE = Path(__file__).resolve().parent.parent
if len(sys.argv) < 2:
    sys.exit(__doc__)
gold_path = Path(sys.argv[1]) if Path(sys.argv[1]).is_absolute() else VAULT / "eval" / sys.argv[1]
top = int(sys.argv[2]) if len(sys.argv) > 2 else 3
cap = sys.argv[3] if len(sys.argv) > 3 else None
gold = json.loads(gold_path.read_text(encoding="utf-8"))
full = found = need = chars = 0
strategies, misses = set(), []
for item in gold:
    cmd = ["python3", str(ENGINE / "scripts" / "retrieve.py"), item["q"], "--top", str(top), "--chunks", "--compact"]
    if cap:
        cmd += ["--per-page-cap", cap]
    out = subprocess.run(cmd, cwd=VAULT, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        sys.exit(f"FAIL rc={out.returncode} q={item['q']}\n{out.stderr[-400:]}")
    d = json.loads(out.stdout)
    strategies.add(d.get("strategy", "unknown"))
    pages = {c["page"] for c in d["candidates"]}
    chars += sum(len(c.get("text", "")) for c in d["candidates"])
    req = set(item["required"])
    got = req & pages
    full += got == req
    found += len(got)
    need += len(req)
    if got != req:
        misses.append((item.get("id", item["q"][:40]), len(got), len(req), sorted(req - got)))
n = len(gold)
print(f"multi-eval --top {top} cap {cap or 'default'}  gold={gold_path.name} n={n}  strategy={', '.join(sorted(strategies))}")
print(f"  allRequired@{top}={full / n:.2f} ({full}/{n})  required pages found {found}/{need} ({found / need:.2f})  "
      f"~{chars / n / 4:.0f} tokens/query")
for ident, g, r, missing in misses:
    print(f"    {ident} {g}/{r}  missing {', '.join(Path(p).name for p in missing)}")
