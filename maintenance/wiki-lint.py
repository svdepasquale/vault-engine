#!/usr/bin/env python3
"""Deterministic lint for knowledge-vault. Emits JSON to stdout.

Findings:
- orphan: page with no inbound wikilink (excludes index/log/hot/overview)
- dead_link: [[X]] target missing
- frontmatter_missing: required fields absent (name, description, type)
- frontmatter_invalid_type: type not in allowed set
- duplicate_title: same file name (slug) in multiple pages
- duplicate_address: same frontmatter `address:` on multiple pages
- stale: updated > 90 days ago; skipped for status evergreen and for the statuses that already mark a page not current (archived, superseded, deprecated, closed, decommissioned, erased), index:false, folds/
- hot_oversize: hot.md > HOT_MAX_BYTES (7168), the budget its injecting hooks check
- log_oversize: log.md > 500 lines (cannot fire since log.md froze at 436 lines, 2026-09-26)
- relation_unknown_predicate: a `relations:` key outside the typed-graph vocabulary
- relation_missing_inverse: an edge between two entity/source pages whose
  inverse predicate is not declared on the target page
- hub_lag [info]: a living entity page unchanged for 14+ days while a page
  that links it changed after it; `read_first` lists the newer linking pages,
  those whose lines added since then name or link it first (full mode only:
  it reads git)

--brief prints one line per finding except the age/size ones (stale,
hot_oversize, log_oversize, hub_lag), nothing when there are none: the form a
session-start hook can show as is. `--brief --only PAGE...` keeps the findings
that involve those pages (as path, referrer or declaring page; `wiki/` prefix
optional): the form a hook uses for the pages a session just changed.
"""
from __future__ import annotations
import os
import json, re, sys, hashlib, subprocess
from datetime import date, datetime
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve() / "wiki"
ALLOWED_TYPES = {"user", "feedback", "entity", "source", "meta"}
SKIP_ORPHAN = {"index.md", "log.md", "hot.md", "overview.md", "_index.md", "WIKI.md"}
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FRONTMATTER = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
# Code is not a link: a page that quotes the syntax (`[[x]]`, a bare `[[`) or
# shows it in a fenced block must not report a dead link, and a bare `[[` must
# not open a match that runs on and swallows the next real link.
# A fence may be indented (inside a list item).
FENCED = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.DOTALL | re.MULTILINE)
INLINE_CODE = re.compile(r"(`+)[^\n]*?(?<!`)\1(?!`)")


def strip_code(body: str) -> str:
    return INLINE_CODE.sub("", FENCED.sub("", body))
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
# hot_oversize has its live alarm in the SessionStart and Stop hooks anyway.
BRIEF_SKIP = {"stale", "hot_oversize", "log_oversize", "hub_lag"}
# Statuses the age check skips: evergreen by declaration, plus every status
# that already marks a page as no longer current (the same set as
# scripts/contextual-prefix.py STALE_STATUSES).
AGELESS_STATUSES = {"evergreen", "archived", "superseded", "deprecated", "closed", "decommissioned", "erased"}
# hot.md reaches the model whole through a SessionStart hook, and Claude Code
# hands a hook's context over 10,000 characters to the model as a 2 KB preview
# (2.1.310: 10,000 whole, 10,001 preview). Bytes track that limit, lines do not:
# 80 lines of prose already pass it. 7 KB leaves room for what the hook appends.
HOT_MAX_BYTES = 7168
# Hub lag (since 2026-11-28): a living entity page left unchanged for
# HUB_QUIET_DAYS while a page that links it changed after it. That is where a
# new fact lands beside the page that should carry it: a version recorded on
# an overview page only, a rule changed where it was decided but not on its
# policy page, an audit's state never carried to the project it audited. On
# its first run, of 12 such pages 7 held stale or missing state, and one of
# those had fewer than three newer linking pages, so one is enough. A page's
# date is its last commit, or its `updated:` when later, or today when it has
# uncommitted changes; a hub reviewed and found current clears by a bump of
# its `updated:`. Info only, and computed in full mode only: it reads git.
HUB_DIRS = ("entities/",)
HUB_QUIET_DAYS = 14
HUB_MIN_NEWER = 1
HUB_READ_FIRST = 8
# Pages that name a hub without facts about it: navigation, the hot cache, the
# frozen log, folds and generated reports.
HUB_LINKER_SKIP = re.compile(r"^(hot|log|index|overview)\.md$|^folds/|^meta/(lint-report|tiling-report)")
NOT_CURRENT_STATUSES = AGELESS_STATUSES - {"evergreen"}

def fm_date(fm: dict) -> date | None:
    """The frontmatter `updated:` as a date (a timestamp's day), None when absent or unreadable."""
    u = fm.get("updated")
    if not u:
        return None
    try:
        return datetime.fromisoformat(u).date() if "T" in u else date.fromisoformat(u)
    except ValueError:
        return None

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)

def git_page_dates(root: Path, wiki_rel: str) -> tuple[dict[str, date], set[str]] | None:
    """Each page's last commit date and the pages with uncommitted changes,
    keyed by their path under the wiki directory, from one `git log` and one
    `git status`. None when the vault root is not the top of its own repository
    (no git, or a directory inside another repository): the dates then come
    from frontmatter only."""
    try:
        top = _git(root, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
            return None
        log = _git(root, "log", "--format=%x00%cs", "--name-only", "--", wiki_rel)
        status = _git(root, "status", "--porcelain", "--untracked-files=all", "--", wiki_rel)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if log.returncode != 0 or status.returncode != 0:
        return None
    prefix = wiki_rel + "/"
    last: dict[str, date] = {}
    for block in log.stdout.split("\x00")[1:]:
        lines = block.strip().splitlines()
        if not lines:
            continue
        day = date.fromisoformat(lines[0])
        for path in lines[1:]:
            if path.startswith(prefix):
                last.setdefault(path[len(prefix):], day)
    dirty = set()
    for line in status.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        if path.startswith(prefix):
            dirty.add(path[len(prefix):])
    return last, dirty

def added_mentions(root: Path, wiki_rel: str, since: date, name: str, paths: list[str]) -> dict[str, int]:
    """Per page, the lines it gained after `since` that name or link `name`:
    one `git diff` from the last commit of that day to the working tree. Empty
    when no commit is that old or git fails."""
    try:
        base = _git(root, "rev-list", "-1", f"--before={since.isoformat()} 23:59:59", "HEAD").stdout.strip()
        if not base:
            return {}
        diff = _git(root, "diff", "--unified=0", "--no-color", "--no-ext-diff", base, "--",
                    *(f"{wiki_rel}/{p}" for p in paths))
    except (OSError, subprocess.TimeoutExpired):
        return {}
    word = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)
    head = f"+++ b/{wiki_rel}/"
    counts: dict[str, int] = {}
    cur = None
    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            cur = line[len(head):] if line.startswith(head) else None
        elif line.startswith("+") and cur and word.search(line):
            counts[cur] = counts.get(cur, 0) + 1
    return counts

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
    if t in ("duplicate_title", "duplicate_address"):
        return f"{t}: {', '.join(f['paths'])}"
    extra = ", ".join(f"{k}={','.join(v) if isinstance(v, list) else v}"
                      for k, v in f.items() if k not in ("type", "path"))
    return f"{t}: {f.get('path', '')}" + (f" ({extra})" if extra else "")

def involves(f: dict) -> set[str]:
    """The pages a finding is about: its own, a declaring page, the referrers."""
    pages = {f.get("path"), f.get("declared_by")} | set(f.get("referrers", [])) | set(f.get("paths", []))
    pages.discard(None)
    return pages

def slug(p: Path) -> str:
    return p.stem

def main(brief: bool = False, only: set[str] | None = None):
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
        body = strip_code(FRONTMATTER.sub("", text, count=1))
        for m in WIKILINK.finditer(body):
            # In a table row the alias pipe is escaped: [[b\|alias]] links b.
            tgt = m.group(1).strip().rstrip("\\").strip()
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

    by_address: dict[str, list[Path]] = {}
    for p, fm in fm_cache.items():
        if fm.get("address"):
            by_address.setdefault(fm["address"], []).append(p)
    for a, ps in by_address.items():
        if len(ps) > 1:
            findings.append({
                "type": "duplicate_address",
                "address": a,
                "paths": sorted(p.relative_to(VAULT).as_posix() for p in ps),
            })

    for p, fm in fm_cache.items():
        rel = p.relative_to(VAULT).as_posix()
        # Age cannot make these stale: evergreen pages and pages already
        # declared not current (the stale statuses contextual-prefix.py labels:
        # a closed or superseded page does not age further), index:false pages
        # (excluded from retrieval, answer nothing), and folds (extractive
        # rollups of dated log entries, historically scoped).
        if (fm.get("status") or "").strip().lower() in AGELESS_STATUSES:
            continue
        if str(fm.get("index", "")).lower() == "false" or rel.startswith("folds/"):
            continue
        d = fm_date(fm)
        if d is None:
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
            if f["type"] not in BRIEF_SKIP and (only is None or involves(f) & only):
                print(brief_line(f))
        return

    # Hub lag (HUB_QUIET_DAYS above). Links count from the body and from the
    # typed relations alike; the hub's own links to itself do not.
    root, wiki_rel = VAULT.parent, VAULT.name
    git = git_page_dates(root, wiki_rel)
    def rel_of(p: Path) -> str:
        return p.relative_to(VAULT).as_posix()
    def page_date(p: Path) -> date | None:
        if git and rel_of(p) in git[1]:
            return TODAY
        days = [d for d in (git[0].get(rel_of(p)) if git else None, fm_date(fm_cache[p])) if d]
        return max(days) if days else None
    dates = {p: page_date(p) for p in pages}
    inbound: dict[Path, set[Path]] = {}
    for tgt, refs in targets.items():
        q = resolve(tgt)
        if q is not None:
            inbound.setdefault(q, set()).update(r for r in refs if r != q)
    for p in pages:
        for tgts in rel_cache[p].values():
            for t in tgts:
                q = resolve(t)
                if q is not None and q != p:
                    inbound.setdefault(q, set()).add(p)
    for p in sorted(pages):
        rel, fm, hd = rel_of(p), fm_cache[p], dates[p]
        if not rel.startswith(HUB_DIRS) or (fm.get("status") or "").strip().lower() in NOT_CURRENT_STATUSES:
            continue
        if str(fm.get("index", "")).lower() == "false" or hd is None or (TODAY - hd).days < HUB_QUIET_DAYS:
            continue
        newer = [q for q in inbound.get(p, ()) if dates[q] is not None and dates[q] > hd
                 and not HUB_LINKER_SKIP.match(rel_of(q))]
        if len(newer) < HUB_MIN_NEWER:
            continue
        counts = added_mentions(root, wiki_rel, hd, slug(p), sorted(rel_of(q) for q in newer)) if git else {}
        newer.sort(key=lambda q: (-counts.get(rel_of(q), 0), -dates[q].toordinal(), rel_of(q)))
        findings.append({"type": "hub_lag", "path": rel, "last_change": hd.isoformat(),
                         "quiet_days": (TODAY - hd).days, "newer_linkers": len(newer),
                         "read_first": [rel_of(q) for q in newer[:HUB_READ_FIRST]]})

    hot = VAULT / "hot.md"
    if hot.exists():
        size = hot.stat().st_size
        if size > HOT_MAX_BYTES:
            findings.append({"type": "hot_oversize", "bytes": size, "max": HOT_MAX_BYTES})

    logf = VAULT / "log.md"
    if logf.exists():
        n = len(logf.read_text().splitlines())
        if n > 500:
            findings.append({"type": "log_oversize", "lines": n})

    # log.md is excluded from the hash. Historically it was written on
    # every maintenance/apply cycle, which made the staleness gate of the
    # proposal pass (removed 2026-11-12) self-invalidate; since 2026-09-26 it is a frozen archive (git log is
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
    argv, only = sys.argv[1:], None
    if "--only" in argv:
        i = argv.index("--only")
        only = {a.removeprefix("wiki/") for a in argv[i + 1:]}
        argv = argv[:i]
    main(brief="--brief" in argv, only=only)
