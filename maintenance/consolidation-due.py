#!/usr/bin/env python3
"""consolidation-due.py — which pages have grown into journals (vault CLAUDE.md §Consolidate).

Deterministic and cheap: no model, file reads plus two git calls per page that
carries `consolidated:`. The SessionEnd worker (dotfiles wiki-autocommit.sh)
runs it after the index refresh and writes the result to
.git/wiki-consolidate.due, which the SessionStart hook turns into one short
section. Empty file = nothing due.

Scope: current-state pages — wiki/entities, wiki/sources, wiki/runbooks — minus
_index.md, stale statuses (archived, superseded, ...) and `index: false`.
Dated syntheses under wiki/meta are journal-shaped by design and are skipped.

Signals, body only:
  kb      size in KB
  dated   H2/H3 headings carrying a YYYY-MM-DD date (each appended session adds one)
  struck  ~~struck~~ spans (superseded text still sitting on the page)
Rule:
  no `consolidated:`        kb >= 15 and (dated >= 5 or struck >= 10)
  `consolidated: D`         grew >= 50 % and >= 5 KB since D, or >= 3 dated headings newer than D
Calibrated 2026-10-06 on a 132-page vault: it flagged the 8 journal pages and
left the large non-journals alone (a 136 KB numbered gotchas catalogue with 2
dated headings, a 35 KB design document with none).

Usage:
  consolidation-due.py            print the due pages as a table
  consolidation-due.py --write    also (re)write .git/wiki-consolidate.due
  consolidation-due.py --all      print every scanned page with its signals
"""
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
DUE_FILE = VAULT / ".git" / "wiki-consolidate.due"
SCOPE = ("wiki/entities", "wiki/sources", "wiki/runbooks")
STALE = {"archived", "superseded", "deprecated", "closed", "decommissioned", "erased"}

FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
DATED_H_RE = re.compile(r"^#{2,3} .*?\b(20\d\d-\d\d-\d\d)", re.M)
STRUCK_RE = re.compile(r"~~(?=\S)(?:(?!~~|\n\n).)+?~~", re.S)  # same as contextual-prefix.py

MIN_KB, MIN_DATED, MIN_STRUCK = 15, 5, 10
REGROW_RATIO, REGROW_KB, REGROW_DATED = 1.5, 5, 3


def field(fm, name):
    m = re.search(rf"^{name}:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", fm, re.M)
    return m.group(1).strip() if m else ""


def size_at(rel, day):
    """Byte size of the page at the last commit on or before `day` (0 if none)."""
    rev = subprocess.run(["git", "-C", str(VAULT), "rev-list", "-1", f"--before={day} 23:59:59", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if not rev:
        return 0
    out = subprocess.run(["git", "-C", str(VAULT), "cat-file", "-s", f"{rev}:{rel}"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def scan():
    rows = []
    for top in SCOPE:
        for p in sorted((VAULT / top).glob("*.md")):
            if p.name == "_index.md":
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            m = FM_RE.match(text)
            fm, body = (m.group(1), text[m.end():]) if m else ("", text)
            if field(fm, "status").lower() in STALE or field(fm, "index").lower() in ("false", "no", "off"):
                continue
            rel = p.relative_to(VAULT).as_posix()
            dates = DATED_H_RE.findall(body)
            row = {"page": rel, "kb": len(body.encode()) / 1024, "dated": len(dates),
                   "struck": len(STRUCK_RE.findall(body)), "consolidated": field(fm, "consolidated"),
                   "reason": ""}
            cons = row["consolidated"]
            if cons:
                then = size_at(rel, cons) / 1024
                newer = sum(d > cons for d in dates)
                if then and row["kb"] >= then * REGROW_RATIO and row["kb"] - then >= REGROW_KB:
                    row["reason"] = f"grew {then:.0f}->{row['kb']:.0f} KB since consolidated {cons}"
                elif newer >= REGROW_DATED:
                    row["reason"] = f"{newer} dated sections since consolidated {cons}"
            elif row["kb"] >= MIN_KB and (row["dated"] >= MIN_DATED or row["struck"] >= MIN_STRUCK):
                row["reason"] = "journal-shaped, never consolidated"
            rows.append(row)
    return rows


def score(r):
    return r["kb"] + 3 * r["dated"] + r["struck"]


def main():
    args = set(sys.argv[1:])
    rows = scan()
    due = sorted((r for r in rows if r["reason"]), key=score, reverse=True)
    shown = sorted(rows, key=score, reverse=True) if "--all" in args else due
    for r in shown:
        print(f"{r['kb']:6.1f} KB {r['dated']:3d} dated {r['struck']:3d} struck  "
              f"{r['page']}  {r['reason'] or '-'}")
    if not shown:
        print("nothing due")
    if "--write" in args:
        DUE_FILE.write_text("".join(
            f"{r['page']}\t{r['kb']:.0f}\t{r['dated']}\t{r['struck']}\t{r['reason']}\n" for r in due),
            encoding="utf-8")


if __name__ == "__main__":
    main()
