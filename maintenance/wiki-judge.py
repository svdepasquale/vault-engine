#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Apply the auto-mode rubric to a proposals file in code (not in a
markdown skill), splitting it into:

  proposals-auto-YYYY-MM-DD.md   — Claude may auto-apply on session start
  proposals-defer-YYYY-MM-DD.md  — flagged for /wiki-apply interactive

This removes the soft-coded judgement from `~/.claude/skills/wiki-apply`
where the rubric was descriptive markdown only. With this script the
gates are deterministic Python; the skill just consumes the buckets.

Reads stdin or file path argument; writes a JSON summary to stdout.

Rubric (high confidence):
- type=update-frontmatter, fields ⊆ {name, type, tags, status:evergreen}
- type=update-frontmatter adding `description` AND ALL of:
    * 40 ≤ len(value) ≤ 120
    * no filler markers
    * target page body > 200 chars (real content)
    * value not equal to title or name verbatim
- type=mark-evergreen on meta/index/_index/profile pages
- target file exists, slug appears in lint report findings,
  proposed type ∈ allowed set

Anything else → defer.
"""
from __future__ import annotations
import os
import argparse, json, re, sys
from pathlib import Path

import yaml

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve() / "wiki"
LINT_DIR = VAULT / ".lint"
ALLOWED_TYPES = {"user", "feedback", "entity", "source", "meta"}

FILLER_MARKERS = [
    "vault", "various", "comprehensive", "centralized", "general",
    "etc.", "contributors", "maintainers", "relevant items",
    "internal or external",
]

PROPOSAL_RE = re.compile(
    r"^### (proposal-\d+)\n(.*?)(?=^### proposal-|\Z)",
    re.DOTALL | re.MULTILINE,
)
ACTION_BLOCK_RE = re.compile(r"```yaml\n(.*?)```", re.DOTALL)
FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)


def read_target(rel_path: str) -> tuple[dict, str] | None:
    """Return (frontmatter_dict, body_text) or None if file missing."""
    p = VAULT / rel_path
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return ({}, text)
    try:
        fm = yaml.safe_load(m.group(2)) or {}
    except yaml.YAMLError:
        fm = {}
    body = text[m.end():]
    return (fm if isinstance(fm, dict) else {}, body)


def parse_proposal_block(body: str) -> dict:
    """Parse fields like 'type: foo' and the action YAML block."""
    out: dict = {"raw": body}
    for line in body.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m and m.group(1) in {"type", "target", "finding", "rationale"}:
            out[m.group(1)] = m.group(2).strip()
    am = ACTION_BLOCK_RE.search(body)
    if am:
        block = am.group(1)
        # Recover from common qwen2.5:7b output artefacts before parsing.
        # All recoveries are conservative: if the block still doesn't
        # parse to a dict we return None and the caller defers.
        # 1. Stray `---` document markers wrapping the action
        block = re.sub(r"^\s*---\s*\n", "", block)
        block = re.sub(r"\n\s*---\s*$", "\n", block)
        # 2. Comment-only first line ("# concrete change spec")
        block = re.sub(r"^\s*#[^\n]*\n", "", block)
        # 3. JSON-style wrapper around a single key (e.g. `{"description": "..."}`)
        block = block.strip()
        if block.startswith("{") and block.endswith("}"):
            try:
                import json as _json
                parsed = _json.loads(block)
                if isinstance(parsed, dict):
                    out["action"] = parsed
                    return out
            except (ValueError, TypeError):
                pass
        try:
            parsed = yaml.safe_load(block)
            out["action"] = parsed if isinstance(parsed, dict) else None
        except yaml.YAMLError:
            out["action"] = None
    return out


def is_meta_page(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    return (
        rel_path.startswith("meta/")
        or rel_path.startswith("entities/_index")
        or rel_path.startswith("sources/_index")
        or name in {"index.md", "_index.md", "overview.md"}
    )


def infer_type_from_path(rel: str) -> str | None:
    name = rel.rsplit("/", 1)[-1]
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


def judge(prop: dict, lint_targets: set[str]) -> tuple[str, str]:
    """Return (verdict, reason) where verdict in {'auto', 'defer'}."""
    ptype = prop.get("type", "").strip()
    target = prop.get("target", "").strip()
    action = prop.get("action")

    # Basic sanity
    if not target:
        return ("defer", "no target")
    if target not in lint_targets:
        return ("defer", "target not in lint findings (possible hallucination)")
    tgt = read_target(target)
    if tgt is None:
        return ("defer", f"target file missing: {target}")
    fm, body = tgt

    # mark-evergreen on meta-ish pages
    if ptype == "mark-evergreen":
        if is_meta_page(target):
            return ("auto", "mark-evergreen on meta page")
        return ("defer", "mark-evergreen on non-meta page")

    if ptype != "update-frontmatter":
        return ("defer", f"non-frontmatter proposal type: {ptype}")

    if not isinstance(action, dict) or not action:
        return ("defer", "action block missing or unparseable")

    # Reject if any field already exists with a non-empty value
    for k, v in action.items():
        if k in fm and fm[k] not in (None, "", [], {}):
            return ("defer", f"field '{k}' already populated in target")

    # Validate proposed type matches path
    if "type" in action:
        if action["type"] not in ALLOWED_TYPES:
            return ("defer", f"invalid type value: {action['type']}")
        path_default = infer_type_from_path(target)
        if path_default and action["type"] != path_default:
            return ("defer", f"type mismatch: proposed {action['type']}, path implies {path_default}")

    # Description gates (option C)
    if "description" in action:
        desc = action["description"]
        if not isinstance(desc, str):
            return ("defer", "description not a string")
        if not (40 <= len(desc) <= 120):
            return ("defer", f"description length {len(desc)} outside [40,120]")
        low = desc.lower()
        for marker in FILLER_MARKERS:
            if marker in low:
                return ("defer", f"description contains filler marker: '{marker}'")
        if len(body.strip()) < 200:
            return ("defer", "target body <200 chars (no content to summarise)")
        title = fm.get("title") if isinstance(fm.get("title"), str) else ""
        name = fm.get("name") if isinstance(fm.get("name"), str) else ""
        if desc.strip() in {title.strip(), name.strip()}:
            return ("defer", "description duplicates title/name verbatim")

    # All other fields (name, type, tags, status:evergreen) — passthrough
    safe_fields = {"name", "type", "tags", "status", "description"}
    extra = set(action.keys()) - safe_fields
    if extra:
        return ("defer", f"action touches non-whitelisted fields: {sorted(extra)}")

    if "status" in action and action["status"] != "evergreen":
        return ("defer", f"status value not 'evergreen': {action['status']}")

    return ("auto", "all gates passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("proposals_file", nargs="?", help="path to proposals-*.md")
    ap.add_argument("--report", default=str(LINT_DIR / "report.json"),
                    help="lint report JSON for target validation")
    args = ap.parse_args()

    if args.proposals_file:
        text = Path(args.proposals_file).read_text(encoding="utf-8")
        src_path = Path(args.proposals_file)
    else:
        text = sys.stdin.read()
        src_path = None

    try:
        report = json.loads(Path(args.report).read_text())
        lint_targets = {f["path"] for f in report.get("findings", []) if "path" in f}
    except (FileNotFoundError, json.JSONDecodeError):
        lint_targets = set()

    blocks = list(PROPOSAL_RE.finditer(text))
    auto, defer = [], []
    for m in blocks:
        pid = m.group(1)
        body = m.group(2)
        prop = parse_proposal_block(body)
        verdict, reason = judge(prop, lint_targets)
        record = {
            "id": pid,
            "type": prop.get("type"),
            "target": prop.get("target"),
            "action": prop.get("action"),
            "verdict": verdict,
            "reason": reason,
            "block": f"### {pid}\n{body}".rstrip() + "\n",
        }
        (auto if verdict == "auto" else defer).append(record)

    summary = {
        "source": str(src_path) if src_path else "<stdin>",
        "total": len(blocks),
        "auto": len(auto),
        "defer": len(defer),
        "details": [
            {k: v for k, v in r.items() if k != "block"} for r in (auto + defer)
        ],
    }

    if src_path:
        date = src_path.stem.replace("proposals-", "")
        if auto:
            (LINT_DIR / f"proposals-auto-{date}.md").write_text(
                "".join(r["block"] for r in auto), encoding="utf-8")
        if defer:
            (LINT_DIR / f"proposals-defer-{date}.md").write_text(
                "".join(r["block"] for r in defer), encoding="utf-8")

    json.dump(summary, sys.stdout, indent=2, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
