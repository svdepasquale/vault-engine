#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Deterministic safe autofix pass for the knowledge-vault.

Runs BEFORE the LLM proposal stage. Handles fixes that need no model
judgement: derived from filename, path, or other frontmatter fields.

Whitelist (only these classes; anything else stays for LLM/review):
- frontmatter `name` missing  → use existing `title` value, else slug
- frontmatter `type` missing  → infer from path:
    entities/         → entity
    sources/          → source
    meta/profile/     → user
    meta/             → meta
    feedback-*.md     → feedback
    (no match)        → SKIP, leave for review
- frontmatter `tags` missing  → set to []

Never touches:
- description (semantic)
- content body
- wikilinks
- file location/name
- log rotation, dedup, merges

Frontmatter is parsed with PyYAML so multi-line values (block
scalars, lists, anchors) are detected as "present" — no false
positives that would duplicate keys and corrupt YAML.

Output: JSON to stdout summarising what was fixed.
Safe to re-run; idempotent.
"""
from __future__ import annotations
import os
import json, re, sys, hashlib
from pathlib import Path

import yaml  # noqa: E402

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve() / "wiki"
ALLOWED_TYPES = {"user", "feedback", "entity", "source", "meta"}
SKIP_PAGES = {"hot.md", "log.md", "index.md", "overview.md", "WIKI.md"}

FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)


def vault_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(VAULT.rglob("*.md")):
        if ".lint" in p.parts:
            continue
        h.update(p.relative_to(VAULT).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def infer_type(rel: str) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    # feedback prefix wins over directory rules (feedback files often
    # live under meta/profile/ but should be type=feedback, not meta/user)
    if name.startswith("feedback-"):
        return "feedback"
    if rel.startswith("entities/"):
        return "entity"
    if rel.startswith("sources/"):
        return "source"
    if rel.startswith("meta/profile/"):
        return "user"
    if rel.startswith("meta/"):
        return "meta"
    return None


def derive_name(fm: dict, path: Path) -> str:
    title = fm.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return path.stem.replace("-", " ").replace("_", " ").strip().title()


def fix_page(path: Path) -> list[str]:
    """Return list of field names that were added. Mutates file in place."""
    if path.name in SKIP_PAGES:
        return []
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return []  # no frontmatter at all → leave for review

    fm_text = m.group(2)
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        # malformed YAML → don't touch, leave for human review
        return []

    if not isinstance(fm, dict):
        return []

    rel = path.relative_to(VAULT).as_posix()
    additions: list[tuple[str, object]] = []

    if "name" not in fm:
        additions.append(("name", derive_name(fm, path)))

    if "type" not in fm:
        inferred = infer_type(rel)
        if inferred:
            additions.append(("type", inferred))

    if "tags" not in fm:
        additions.append(("tags", []))

    if not additions:
        return []

    # Insert new keys at the top of frontmatter, preserve existing block.
    # We render new keys with yaml.safe_dump for proper escaping, then
    # prepend to the original frontmatter body verbatim (no round-trip
    # through yaml.dump on the existing block — that would lose comments,
    # ordering, and anchor styles users hand-author).
    insert_block = yaml.safe_dump(
        dict(additions), sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip("\n")
    new_fm = insert_block + "\n" + fm_text
    new_text = m.group(1) + new_fm + m.group(3) + text[m.end():]

    # Sanity: re-parse merged frontmatter; abort if invalid
    try:
        merged = yaml.safe_load(new_fm)
        if not isinstance(merged, dict):
            return []
    except yaml.YAMLError:
        return []

    path.write_text(new_text, encoding="utf-8")
    return [k for k, _ in additions]


def main():
    sha_before = vault_hash()
    fixed = []
    for p in VAULT.rglob("*.md"):
        if ".lint" in p.parts:
            continue
        added = fix_page(p)
        if added:
            fixed.append({"path": p.relative_to(VAULT).as_posix(), "fields": added})

    out = {
        "vault_sha_before": sha_before,
        "vault_sha_after": vault_hash() if fixed else sha_before,
        "fixed_count": len(fixed),
        "fixed": fixed,
    }
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
