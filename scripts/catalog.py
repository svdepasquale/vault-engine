#!/usr/bin/env python3
"""catalog.py — every wiki page with its one-line description, printed on demand.

The read protocol's fallback when retrieval is unavailable or returns nothing,
and the overview a maintenance round reads. It is built from each page's own
frontmatter at call time: nothing is stored, so nothing can drift from the
pages (hand-kept section index pages did — an entry was a second copy of the
page's `description`, and new pages went unlisted).

    catalog.py                 every section
    catalog.py SECTION ...     only those folders under wiki/ ("runbooks", "meta/profile")
    catalog.py --names         paths only, no descriptions

A line is `- <path under wiki/, no .md> — <description>`, followed by
`[status]` for a page that is not live and `[not indexed]` for one retrieval
never returns (`index: false`) — the catalog is the only way to find those.
No index, no embedder, no network: it reads `wiki/**.md` and nothing else.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
WIKI_DIR = VAULT_ROOT / "wiki"

# Navigation and session files, not knowledge pages.
SKIP_NAMES = {"index.md", "_index.md", "hot.md", "log.md"}
LIVE_STATUSES = {"", "active", "evergreen", "stable", "current", "live"}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
INDEX_OFF_RE = re.compile(r"^(false|no|off)$", re.IGNORECASE)
BLOCK_MARKS = {">", ">-", ">+", "|", "|-", "|+"}


def frontmatter(text: str) -> dict[str, str]:
    """Top-level scalar keys of the frontmatter; a folded or literal block
    scalar (`key: >-` and indented lines) is joined into one line."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    key, block = None, False
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t"):
            if key and block:
                out[key] = (out[key] + " " + line.strip()).strip()
            continue
        key, block = None, False
        if ":" not in line or line.startswith("#"):
            continue
        k, v = line.split(":", 1)
        key, v = k.strip(), v.strip()
        if v in BLOCK_MARKS:
            block, v = True, ""
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[key] = v
    return out


def entries(sections: list[str] | None = None):
    """(section, page id, description, marks) for every knowledge page, sorted."""
    want = [s.strip("/") for s in sections or []]
    rows = []
    for path in sorted(WIKI_DIR.rglob("*.md")):
        rel = path.relative_to(WIKI_DIR)
        if path.name in SKIP_NAMES or any(part.startswith(".") for part in rel.parts):
            continue
        section = rel.parent.as_posix() if rel.parent.as_posix() != "." else "(root)"
        if want and section not in want:
            continue
        try:
            fm = frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        marks = []
        status = fm.get("status", "").strip().lower()
        if status not in LIVE_STATUSES:
            marks.append(status)
        if INDEX_OFF_RE.match(fm.get("index", "")):
            marks.append("not indexed")
        desc = re.sub(r"\s+", " ", fm.get("description", "")).strip() or "(no description)"
        rows.append((section, rel.with_suffix("").as_posix(), desc, marks))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def render(rows, names_only: bool = False) -> str:
    lines, current = [], None
    counts: dict[str, int] = {}
    for section, *_ in rows:
        counts[section] = counts.get(section, 0) + 1
    for section, page, desc, marks in rows:
        if section != current:
            if lines:
                lines.append("")
            lines.append(f"## {section} ({counts[section]})")
            current = section
        tail = "".join(f" [{m}]" for m in marks)
        lines.append(f"- {page}{tail}" if names_only else f"- {page} — {desc}{tail}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="List every wiki page with its description.")
    ap.add_argument("sections", nargs="*", help="folders under wiki/ to list (default: all)")
    ap.add_argument("--names", action="store_true", help="page paths only")
    args = ap.parse_args()
    if not WIKI_DIR.is_dir():
        print(f"catalog: no wiki directory at {WIKI_DIR}", file=sys.stderr)
        return 2
    rows = entries(args.sections)
    if not rows:
        print("catalog: no pages" + (f" in {', '.join(args.sections)}" if args.sections else ""), file=sys.stderr)
        return 1
    print(render(rows, args.names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
