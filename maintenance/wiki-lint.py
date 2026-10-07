#!/usr/bin/env python3
"""Deterministic lint for knowledge-vault. Emits JSON to stdout.

Findings:
- orphan: page with no inbound wikilink (excludes index/log/hot/overview)
- dead_link: [[X]] target missing
- frontmatter_missing: required fields absent (name, description, type)
- frontmatter_invalid_type: type not in allowed set
- duplicate_title: same H1/name in multiple pages
- stale: updated > 90 days ago; skipped for status evergreen/archived, index:false, folds/
- hot_oversize: hot.md > 150 lines
- log_oversize: log.md > 500 lines (cannot fire since log.md froze at 436 lines, 2026-09-26)
"""
from __future__ import annotations
import os
import json, re, sys, hashlib
from datetime import date, datetime
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve() / "wiki"
ALLOWED_TYPES = {"user", "feedback", "entity", "source", "meta"}
SKIP_ORPHAN = {"index.md", "log.md", "hot.md", "overview.md", "_index.md", "WIKI.md"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
TODAY = date.today()

def parse_fm(text: str) -> dict:
    m = FRONTMATTER.match(text)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"\'')
    return fm

def slug(p: Path) -> str:
    return p.stem

def main():
    pages = [p for p in VAULT.rglob("*.md") if ".lint" not in p.parts]
    by_slug: dict[str, list[Path]] = {}
    fm_cache: dict[Path, dict] = {}
    targets: dict[str, set[Path]] = {}  # wikilink target -> referrers

    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        fm_cache[p] = parse_fm(text)
        by_slug.setdefault(slug(p), []).append(p)
        body = FRONTMATTER.sub("", text, count=1)
        for m in WIKILINK.finditer(body):
            tgt = m.group(1).strip()
            targets.setdefault(tgt, set()).add(p)

    findings = []

    for p in pages:
        rel = p.relative_to(VAULT).as_posix()
        if p.name in SKIP_ORPHAN:
            continue
        # A page is linked by its bare slug ([[x]]) or by its vault-relative
        # path ([[runbooks/x]]) — Obsidian resolves both, and the dead-link
        # check below already accepts both. Until 2026-09-26 only the slug
        # counted here, so path-style links left pages falsely orphaned
        # (a runbook linked three times from one entity page plus the index).
        if slug(p) not in targets and p.relative_to(VAULT).with_suffix("").as_posix() not in targets:
            findings.append({"type": "orphan", "path": rel})

    rel_paths = {p.relative_to(VAULT).with_suffix("").as_posix() for p in pages}
    # Obsidian Bases (.base) are valid wikilink/embed targets. They are not *.md
    # so they never enter `pages`; register them explicitly to stop misflagging
    # `[[meta/relations.base]]` etc. as dead_link.
    base_targets: set[str] = set()
    for bp in VAULT.rglob("*.base"):
        if ".lint" in bp.parts:
            continue
        rp = bp.relative_to(VAULT)
        base_targets.add(rp.as_posix())                  # meta/relations.base
        base_targets.add(rp.with_suffix("").as_posix())  # meta/relations
        base_targets.add(bp.stem)                        # relations
    for tgt, refs in targets.items():
        if tgt in by_slug:
            continue
        if tgt in rel_paths:
            continue
        if tgt in base_targets:
            continue
        findings.append({
            "type": "dead_link",
            "target": tgt,
            "referrers": [r.relative_to(VAULT).as_posix() for r in refs],
        })

    for p, fm in fm_cache.items():
        rel = p.relative_to(VAULT).as_posix()
        if p.name in {"hot.md", "log.md", "index.md", "overview.md", "WIKI.md"}:
            continue
        missing = [k for k in ("name", "description", "type") if k not in fm]
        if missing:
            findings.append({"type": "frontmatter_missing", "path": rel, "missing": missing})
        if "type" in fm and fm["type"] not in ALLOWED_TYPES:
            findings.append({"type": "frontmatter_invalid_type", "path": rel, "value": fm["type"]})

    for s, ps in by_slug.items():
        if s == "_index":
            continue
        if len(ps) > 1:
            findings.append({
                "type": "duplicate_title",
                "slug": s,
                "paths": [p.relative_to(VAULT).as_posix() for p in ps],
            })

    for p, fm in fm_cache.items():
        rel = p.relative_to(VAULT).as_posix()
        # Age cannot make these stale: evergreen/archived pages by declaration,
        # index:false pages (excluded from retrieval, answer nothing), and
        # folds (extractive rollups of dated log entries, historically scoped).
        if fm.get("status") in ("evergreen", "archived"):
            continue
        if str(fm.get("index", "")).lower() == "false" or rel.startswith("folds/"):
            continue
        u = fm.get("updated")
        if not u:
            continue
        try:
            d = datetime.fromisoformat(u).date() if "T" in u else date.fromisoformat(u)
        except ValueError:
            continue
        age = (TODAY - d).days
        if age > 90:
            findings.append({"type": "stale", "path": rel, "age_days": age})

    hot = VAULT / "hot.md"
    if hot.exists():
        n = len(hot.read_text().splitlines())
        if n > 150:
            findings.append({"type": "hot_oversize", "lines": n})

    logf = VAULT / "log.md"
    if logf.exists():
        n = len(logf.read_text().splitlines())
        if n > 500:
            findings.append({"type": "log_oversize", "lines": n})

    # log.md is excluded from the hash. Historically it was written on
    # every maintenance/apply cycle, which made the PENDING staleness gate
    # self-invalidate; since 2026-09-26 it is a frozen archive (git log is
    # the operation log), and the exclusion is kept as harmless.
    vault_hash = hashlib.sha256()
    for p in sorted(pages):
        rel = p.relative_to(VAULT).as_posix()
        if rel == "log.md":
            continue
        vault_hash.update(rel.encode())
        vault_hash.update(p.read_bytes())

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vault_sha": vault_hash.hexdigest()[:16],
        "page_count": len(pages),
        "findings": findings,
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()

if __name__ == "__main__":
    main()
