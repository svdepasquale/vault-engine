#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""fm-validate.py — does YAML read a page's frontmatter the way the line readers do?

Most vault scripts read frontmatter line by line (wiki-lint, contextual-prefix,
consolidation-due); wiki-autofix, wiki-judge and outside tools parse it as YAML.
The two readings can disagree with neither one failing: an unquoted ` #` starts a
YAML comment, so YAML keeps the text before it while the line readers keep the
whole line (it happened: a batch of descriptions lost their tails that way and
nothing noticed). This script parses each block with PyYAML and reports:

  no_frontmatter  the page does not open with a --- block
  yaml_error      the block does not parse (the parser's line and reason)
  not_mapping     it parses to something other than key: value pairs
  duplicate_key   a key appears twice (YAML keeps the last, some regexes the first)
  value_mismatch  a top-level value reads differently to YAML than to the line
                  readers: a trailing ` # …`, a value continued on the next line
  bad_shape       tags not a list of strings; relations not a map of predicate →
                  string or list of strings

The page contract — required keys, types, the typed-graph vocabulary — stays in
wiki-lint.py; this script only checks that both kinds of reader see the same page.

Usage:
  fm-validate.py [PATH ...]   pages, absolute or relative to the vault
                              (default: every page under wiki/)
  fm-validate.py --json ...   one JSON object per finding
Exit: 0 clean, 1 findings, 2 a path that is not a file.
"""
import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import yaml

# Data and code are separate repos: this file belongs to vault-engine, the vault
# it works on is WIKI_VAULT (default ~/projects/knowledge-vault).
VAULT_ROOT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
TOP_KEY = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")
# Values the line readers cannot see as one scalar anyway: flow and block
# collections, block scalars, anchors, aliases and tags.
NOT_SCALAR = ("[", "{", "|", ">", "&", "*", "!")


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def line_view(block):
    """{key: raw value} as the line readers see a top-level `key: value` line."""
    out = {}
    for line in block.splitlines():
        m = TOP_KEY.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def as_line_reader_sees(raw):
    return raw.strip('"\'')


def as_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, int, float, str)):
        return str(value)
    return None


def check_text(text, rel):
    m = FRONTMATTER.match(text)
    if not m:
        return [{"kind": "no_frontmatter", "path": rel}]
    block = m.group(1)
    try:
        data = yaml.load(block, Loader=UniqueKeyLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        where = f"frontmatter line {mark.line + 1}: " if mark is not None else ""
        reason = getattr(e, "problem", None) or str(e).splitlines()[0]
        kind = "duplicate_key" if "duplicate key" in str(reason) else "yaml_error"
        return [{"kind": kind, "path": rel, "detail": f"{where}{reason}"}]
    if data is None:
        return []
    if not isinstance(data, dict):
        return [{"kind": "not_mapping", "path": rel, "detail": type(data).__name__}]

    findings = []
    lines = line_view(block)
    for key, raw in lines.items():
        if key not in data or not raw or raw.startswith(NOT_SCALAR):
            continue
        yaml_text = as_text(data[key])
        if yaml_text is None:
            continue
        seen = as_line_reader_sees(raw)
        if seen != yaml_text and seen != yaml_text.strip('"\''):
            findings.append({"kind": "value_mismatch", "path": rel, "key": key,
                             "detail": f"YAML reads {yaml_text!r}, the line readers {seen!r}; "
                                       f"quote the value on one line"})

    tags = data.get("tags")
    if "tags" in data and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        findings.append({"kind": "bad_shape", "path": rel, "key": "tags",
                         "detail": "expected a list of strings"})
    rels = data.get("relations")
    if "relations" in data:
        ok = isinstance(rels, dict) and all(
            isinstance(k, str) and (isinstance(v, str) or
                                    (isinstance(v, list) and all(isinstance(t, str) for t in v)))
            for k, v in rels.items())
        if not ok:
            findings.append({"kind": "bad_shape", "path": rel, "key": "relations",
                             "detail": "expected predicate: \"[[page]]\" or predicate: [\"[[a]]\", \"[[b]]\"]"})
    return findings


def rel_path(p):
    try:
        return p.resolve().relative_to(VAULT_ROOT).as_posix()
    except ValueError:
        return str(p)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help="pages (default: every page under wiki/)")
    ap.add_argument("--json", action="store_true", help="one JSON object per finding")
    args = ap.parse_args(argv)

    if args.paths:
        pages = [Path(p) if Path(p).is_absolute() else VAULT_ROOT / p for p in args.paths]
        missing = [str(p) for p in pages if not p.is_file()]
        if missing:
            print(f"not a file: {', '.join(missing)}", file=sys.stderr)
            return 2
    else:
        pages = sorted(p for p in (VAULT_ROOT / "wiki").rglob("*.md") if ".lint" not in p.parts)

    findings = []
    for p in pages:
        findings.extend(check_text(p.read_text(encoding="utf-8", errors="replace"), rel_path(p)))
    for f in findings:
        if args.json:
            print(json.dumps(f, ensure_ascii=False))
        else:
            key = f" {f['key']}" if "key" in f else ""
            detail = f": {f['detail']}" if "detail" in f else ""
            print(f"{f['kind']}: {f['path']}{key}{detail}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
