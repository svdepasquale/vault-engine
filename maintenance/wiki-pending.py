#!/usr/bin/env python3
"""Per-target staleness gate for wiki-apply proposals.

Replaces the whole-vault sha gate (schema 1), which compared a hash of
every page in the vault against the hash taken when proposals were
generated. That gate was correct in intent and useless in practice: it
fired on ANY vault write, including the bookkeeping writes the wiki
pipeline makes itself. `log.md` was exempted on 2026-06-10 for exactly
this reason, but `hot.md` has the same property (27 commits in the 30
days to 2026-08-30) and never was, so /wiki-apply was blocked at almost
every session start by drift on pages no proposal referenced.

Schema 2 hashes only the pages the pending proposals actually target.
A proposal is stale when ITS target moved, which is the only drift that
can make it wrong. Unrelated vault activity is ignored.

  wiki-pending.py write   parse targets from .lint/proposals-*.md,
                          hash each one now, write .lint/PENDING
  wiki-pending.py check   re-hash those targets, report which moved

check exit codes: 0 all fresh · 3 some stale · 4 legacy PENDING
(schema 1, migrate with `write`) · 2 nothing to check.
"""
from __future__ import annotations
import os
import argparse, hashlib, json, re, sys
from datetime import datetime
from pathlib import Path

# Data and code are separate repos since 2026-10-06: this file belongs to vault-engine,
# the vault it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve() / "wiki"
LINT_DIR = VAULT / ".lint"
SCHEMA = 2

BLOCK_RE = re.compile(
    r"^### (proposal-\d+)\n(.*?)(?=^### proposal-|\Z)", re.DOTALL | re.MULTILINE
)
FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
TARGET_RE = re.compile(r"^target:\s*(.+?)\s*$", re.MULTILINE)
LEGACY_RE = re.compile(r"^[0-9a-f]{16}$")


def hash_file(p: Path) -> str | None:
    """16-hex digest of a page, or None when it does not exist.

    None is a real value, not an error: a proposal may target a page the
    model invented. Absent-then-absent is fresh; absent-then-present is
    drift like any other.
    """
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def proposal_files(lint_dir: Path) -> list[Path]:
    """Top-level proposal files only — archive/ is deliberately not globbed."""
    return sorted(p for p in lint_dir.glob("proposals-*.md") if p.is_file())


def parse_targets(text: str) -> set[str]:
    """Targets declared by proposal blocks, ignoring fenced action YAML."""
    out: set[str] = set()
    for m in BLOCK_RE.finditer(text):
        body = FENCE_RE.sub("", m.group(2))
        tm = TARGET_RE.search(body)
        if tm:
            out.add(tm.group(1).strip())
    return out


def collect_targets(lint_dir: Path) -> tuple[set[str], list[str]]:
    targets: set[str] = set()
    sources: list[str] = []
    for f in proposal_files(lint_dir):
        found = parse_targets(f.read_text(encoding="utf-8", errors="replace"))
        if found:
            sources.append(f.name)
        targets |= found
    return targets, sources


def report_sha(lint_dir: Path) -> str | None:
    """vault_sha from the lint report, kept for correlation only.

    Recorded but never gated on: it is the number whose churn caused the
    schema-1 false positives.
    """
    try:
        return json.loads((lint_dir / "report.json").read_text()).get("vault_sha")
    except (OSError, ValueError):
        return None


def cmd_write(lint_dir: Path, vault: Path) -> int:
    files = proposal_files(lint_dir)
    if not files:
        json.dump({"error": "no proposals-*.md in .lint"}, sys.stdout, indent=2)
        print()
        return 2
    targets, sources = collect_targets(lint_dir)
    state = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vault_sha": report_sha(lint_dir),
        "sources": sources,
        "targets": {t: hash_file(vault / t) for t in sorted(targets)},
    }
    (lint_dir / "PENDING").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    json.dump({"written": str(lint_dir / "PENDING"), **state}, sys.stdout, indent=2,
              ensure_ascii=False)
    print()
    return 0


def cmd_check(lint_dir: Path, vault: Path) -> int:
    pending = lint_dir / "PENDING"
    if not pending.is_file():
        json.dump({"error": "no PENDING flag"}, sys.stdout, indent=2)
        print()
        return 2

    raw = pending.read_text(encoding="utf-8").strip()
    if LEGACY_RE.match(raw):
        json.dump({
            "schema": 1, "legacy": True, "ok": False, "vault_sha": raw,
            "reason": "legacy whole-vault PENDING — migrate with "
                      "`wiki-pending.py write`",
        }, sys.stdout, indent=2)
        print()
        return 4

    try:
        state = json.loads(raw)
    except ValueError:
        json.dump({"error": "PENDING is neither schema-1 hex nor JSON"},
                  sys.stdout, indent=2)
        print()
        return 2

    recorded = state.get("targets") or {}
    stale, fresh = [], []
    for path, was in sorted(recorded.items()):
        now = hash_file(vault / path)
        if now == was:
            fresh.append(path)
            continue
        if was is None:
            reason = "target appeared since proposals were generated"
        elif now is None:
            reason = "target deleted since proposals were generated"
        else:
            reason = "target modified since proposals were generated"
        stale.append({"path": path, "was": was, "now": now, "reason": reason})

    out = {
        "schema": state.get("schema", SCHEMA),
        "ok": not stale,
        "generated_at": state.get("generated_at"),
        "checked": len(recorded),
        "fresh": fresh,
        "stale": stale,
    }
    if not recorded:
        out["warning"] = "PENDING records no targets — gate is vacuous"
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 3 if stale else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["write", "check"])
    ap.add_argument("--vault", default=str(VAULT))
    ap.add_argument("--lint-dir", default=None)
    args = ap.parse_args()

    vault = Path(args.vault)
    lint_dir = Path(args.lint_dir) if args.lint_dir else vault / ".lint"
    return cmd_write(lint_dir, vault) if args.command == "write" else cmd_check(lint_dir, vault)


if __name__ == "__main__":
    sys.exit(main())
