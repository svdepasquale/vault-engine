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
- relation_unknown_predicate: a `relations:` key outside the typed-graph vocabulary
- relation_missing_inverse: an edge between two entity/source pages whose
  inverse predicate is not declared on the target page

--brief prints one line per finding except the age/size ones (stale,
hot_oversize, log_oversize), nothing when there are none: the form a
session-start hook can show as is.
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

# Typed-graph vocabulary (the vault's CLAUDE.md §Typed graph). A predicate with
# an inverse is declared on both pages, or a reader of the target page never
# sees the edge. Only entity and source pages carry `relations:`, so an edge
# into any other page type is one-way by schema and needs no inverse there.
INVERSE = {
    "hosted_on": "hosts", "monitored_by": "monitors", "documented_in": "documents",
    "replaces": "replaced_by", "blocks": "blocked_by", "consumed_by": "consumes",
    "managed_by": "manages", "references": "referenced_by", "implements": "implemented_by",
}
INVERSE.update({inv: fwd for fwd, inv in list(INVERSE.items())})
PREDICATES = set(INVERSE) | {"depends_on", "part_of", "related_to"}
GRAPH_TYPES = {"entity", "source"}
REL_KEY = re.compile(r"^(\s+)([A-Za-z_][\w-]*):(.*)$")
# Age and size findings are maintenance signals, not write errors: --brief skips them.
BRIEF_SKIP = {"stale", "hot_oversize", "log_oversize"}

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

def strip_comment(line: str) -> str:
    """Drop a YAML comment: a `#` after whitespace, outside quotes (the `#` of
    a `[[page#section]]` link follows a letter, so it survives)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line

def parse_relations(text: str) -> dict[str, list[str]]:
    """The frontmatter's `relations:` block as {predicate: [wikilink targets]}.

    Line-based like parse_fm, so no YAML dependency: a predicate is a key at
    the block's first indent level, and its targets are the wikilinks on its
    own line (scalar or flow list) and on the list lines below it. A top-level
    `replaced_by:` (allowed on archived pages) counts as the same predicate.
    """
    m = FRONTMATTER.match(text)
    if not m:
        return {}
    rels: dict[str, list[str]] = {}
    in_block, key_indent, cur = False, None, None
    for line in m.group(1).splitlines():
        line = strip_comment(line).rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            key = line.partition(":")[0].strip()
            in_block, cur = key == "relations", None
            if key == "replaced_by":
                rels.setdefault(key, []).extend(t.strip() for t in WIKILINK.findall(line))
            continue
        if not in_block:
            continue
        km = REL_KEY.match(line)
        if km and (key_indent is None or len(km.group(1)) <= key_indent):
            key_indent, cur = len(km.group(1)), km.group(2)
            rels.setdefault(cur, [])
            line = km.group(3)
        if cur is not None:
            rels[cur].extend(t.strip() for t in WIKILINK.findall(line))
    return rels

def brief_line(f: dict) -> str:
    t = f["type"]
    if t == "relation_missing_inverse":
        return (f"{t}: {f['path']} lacks `{f['predicate']}: [[{f['target']}]]`"
                f" ({f['declared_by']} declares `{f['declared']}`)")
    if t == "dead_link":
        return f"{t}: [[{f['target']}]] in {', '.join(f['referrers'])}"
    if t == "duplicate_title":
        return f"{t}: {', '.join(f['paths'])}"
    extra = ", ".join(f"{k}={','.join(v) if isinstance(v, list) else v}"
                      for k, v in f.items() if k not in ("type", "path"))
    return f"{t}: {f.get('path', '')}" + (f" ({extra})" if extra else "")

def slug(p: Path) -> str:
    return p.stem

def main(brief: bool = False):
    pages = [p for p in VAULT.rglob("*.md") if ".lint" not in p.parts]
    by_slug: dict[str, list[Path]] = {}
    fm_cache: dict[Path, dict] = {}
    rel_cache: dict[Path, dict[str, list[str]]] = {}
    targets: dict[str, set[Path]] = {}  # wikilink target -> referrers

    for p in pages:
        text = p.read_text(encoding="utf-8", errors="replace")
        fm_cache[p] = parse_fm(text)
        rel_cache[p] = parse_relations(text)
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

    # Typed relations. The dead-link check above reads the body only, and a
    # relation target with no page yet is an allowed placeholder: it is skipped
    # here too, as is a slug shared by two pages (duplicate_title covers it).
    page_by_path = {p.relative_to(VAULT).with_suffix("").as_posix(): p for p in pages}
    def resolve(target: str) -> Path | None:
        if target in page_by_path:
            return page_by_path[target]
        same = by_slug.get(target, [])
        return same[0] if len(same) == 1 else None

    edges: dict[Path, dict[str, set[Path]]] = {}
    for p in pages:
        for pred, tgts in rel_cache[p].items():
            if pred not in PREDICATES:
                findings.append({"type": "relation_unknown_predicate",
                                 "path": p.relative_to(VAULT).as_posix(), "predicate": pred})
                continue
            for t in tgts:
                q = resolve(t)
                if q is not None and q != p:
                    edges.setdefault(p, {}).setdefault(pred, set()).add(q)
    for p in sorted(edges):
        if fm_cache[p].get("type") not in GRAPH_TYPES:
            continue
        for pred in sorted(edges[p]):
            inv = INVERSE.get(pred)
            if inv is None:
                continue
            for q in sorted(edges[p][pred]):
                if fm_cache[q].get("type") in GRAPH_TYPES and p not in edges.get(q, {}).get(inv, set()):
                    findings.append({
                        "type": "relation_missing_inverse",
                        "path": q.relative_to(VAULT).as_posix(),
                        "predicate": inv,
                        "target": slug(p),
                        "declared_by": p.relative_to(VAULT).as_posix(),
                        "declared": pred,
                    })

    if brief:
        for f in findings:
            if f["type"] not in BRIEF_SKIP:
                print(brief_line(f))
        return

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
    main(brief="--brief" in sys.argv[1:])
