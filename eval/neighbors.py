#!/usr/bin/env python3
"""neighbors.py — where the answer sits when the right page comes back with the wrong chunk.

A diagnostic beside run-eval.py, not part of the gate. It drives the shipped
scripts/retrieve.py the way run-eval.py does (--top 3 --chunks --compact) over
gold sets whose items carry answer markers ("current"), labels each returned
chunk the same way (markers matched after dropping `*` and backticks), and for
every item whose window holds a gold page but no answer chunk it reports where
on that page the answer is and what each cheaper fallback than a full-page read
would cost: chunk ±1, chunk ±2, the H2 section of the returned chunk.

Why it exists (2026-11-07): a sibling project shipped section reads bound to a
page hash, on the premise that an insufficient chunk sends the agent to a
full-page read. The question for this engine was whether a line range per
chunk would pay off. This script answers the gold-set half of it (how often
the answer is next door); the other half was a pass over session transcripts.
Re-run it when chunking or the per-page cap changes.

Usage:
  python3 eval/neighbors.py                                  # the four standard + KU sets
  python3 eval/neighbors.py --gold goldset-it.json           # one set (relative to the vault's eval/)
  python3 eval/neighbors.py --save /tmp/neighbors.json       # keep the per-item rows

Read-only: it never writes to the vault. The vault is WIKI_VAULT (default
~/projects/knowledge-vault); it prints the BM25 index's content hash before and
after, the same hash run-eval.py prints, so a rebuild mid-run shows.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_VAULT") or Path.home() / "projects" / "knowledge-vault").resolve()
ENGINE = Path(__file__).resolve().parent.parent
CHUNKS = VAULT / ".vault-meta" / "chunks"
BM25 = VAULT / ".vault-meta" / "bm25" / "index.json"
DEFAULT_SETS = ["goldset.json", "goldset-it.json", "goldset-ku.json", "goldset-ku-it.json"]
H2 = re.compile(r"(?m)^## .*$")


def norm(s):
    return re.sub(r"[*`]", "", s).lower()


def index_sha():
    """Content hash of the BM25 index, computed as run-eval.py does."""
    if not BM25.is_file():
        return "none"
    docs = json.loads(BM25.read_text(encoding="utf-8")).get("docs", {})
    key = sorted((cid, d.get("input_hash") or d.get("path"), d.get("dl")) for cid, d in docs.items())
    return hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()[:12]


def load_chunks():
    pages = defaultdict(list)
    for f in CHUNKS.glob("*/chunk-*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        pages[d["page_path"]].append(d)
    for chunks in pages.values():
        chunks.sort(key=lambda d: d["chunk_index"])
    return pages


def sections(page, chunks):
    """The H2 heading in force where each chunk starts, located in the page source."""
    src = (VAULT / page).read_text(encoding="utf-8")
    heads = [(m.start(), m.group(0)) for m in H2.finditer(src)]
    out, pos = {}, 0
    for c in chunks:
        probe = c["raw_text"].strip()[:120]
        at = src.find(probe, pos) if probe else -1
        if at < 0 and probe:
            at = src.find(probe)
        if at >= 0:
            pos = at
        first = c["raw_text"].lstrip().split("\n", 1)[0]
        head = first if first.startswith("## ") else None
        if head is None:
            for start, text in heads:
                if start <= pos + 2:
                    head = text
        out[c["chunk_index"]] = head
    return out, len(src), src.count("\n") + 1


def analyse(item, pages):
    out = subprocess.run(["python3", str(ENGINE / "scripts" / "retrieve.py"), item["q"],
                          "--top", "3", "--chunks", "--compact"],
                         cwd=VAULT, capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        return {"q": item["q"], "case": f"retrieve failed (exit {out.returncode})"}
    cands = json.loads(out.stdout)["candidates"]
    markers = [norm(m) for m in item["current"]]
    labels = "".join("C" if any(m in norm(c.get("text", "")) for m in markers) else "-" for c in cands)
    row = {"q": item["q"], "labels": labels}
    if "C" in labels:
        row["case"] = "answer in window"
        return row
    right = [c for c in cands if c["page"] in set(item["gold"])]
    if not right:
        row.update(case="page miss", got=[c["page"] for c in cands])
        return row
    page = right[0]["page"]
    chunks = pages.get(page, [])
    by_idx = {c["chunk_index"]: c for c in chunks}
    answer = [c["chunk_index"] for c in chunks if any(m in norm(c["raw_text"]) for m in markers)]
    returned = sorted({int(c["chunk_id"].split(":")[1]) for c in right if c["page"] == page})
    sec, page_chars, page_lines = sections(page, chunks)
    row.update(case="right page, wrong chunk", page=page, returned=returned, answer=answer,
               n_chunks=len(chunks), page_chars=page_chars,
               # what a whole-page Read returns: the text plus a line-number prefix per line
               read_cost=page_chars + 7 * page_lines)
    if answer:
        row["dist"] = min(abs(r - a) for r in returned for a in answer)
        row["same_section"] = any(sec.get(a) == sec.get(r) for r in returned for a in answer)
    for k in (1, 2):
        window = {i for r in returned for i in range(r - k, r + k + 1)} & set(by_idx)
        row[f"extra_pm{k}"] = sum(len(by_idx[i]["raw_text"]) for i in window - set(returned))
    own = {sec.get(r) for r in returned}
    row["extra_section"] = sum(len(c["raw_text"]) for c in chunks
                               if sec.get(c["chunk_index"]) in own and c["chunk_index"] not in returned)
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", action="append", help="gold set (repeatable; relative to the vault's eval/)")
    ap.add_argument("--save", metavar="PATH", help="write the per-item rows as JSON")
    args = ap.parse_args()
    sets = args.gold or DEFAULT_SETS
    pages = load_chunks()
    before = index_sha()
    report = {}
    for name in sets:
        path = Path(name) if Path(name).is_absolute() else VAULT / "eval" / name
        items = [i for i in json.loads(path.read_text(encoding="utf-8")) if i.get("current")]
        report[path.name] = [analyse(i, pages) for i in items]
    after = index_sha()
    print(f"index {before} -> {after}" + ("   WARN: rebuilt during the run" if before != after else ""))
    for name, rows in report.items():
        wrong = [r for r in rows if r["case"] == "right page, wrong chunk"]
        miss = [r for r in rows if r["case"] == "page miss"]
        hit = sum(r["case"] == "answer in window" for r in rows)
        print(f"\n{name}: {len(rows)} items with markers · answer in window {hit} · "
              f"page miss {len(miss)} · right page, wrong chunk {len(wrong)}")
        for r in wrong:
            print(f"  {r['labels']:<4} dist={r.get('dist')} same_section={r.get('same_section')} "
                  f"returned={r['returned']} answer={r['answer']} of {r['n_chunks']} chunks · "
                  f"extra ±1 {r['extra_pm1']} · ±2 {r['extra_pm2']} · section {r['extra_section']} · "
                  f"page read {r['read_cost']} chars\n      {r['page']}  {r['q']}")
        for r in miss:
            print(f"  page miss {r['labels']:<4} {r['q']}   got {r['got']}")
        for r in rows:
            if r["case"].startswith("retrieve failed"):
                print(f"  {r['case']}: {r['q']}")
    if args.save:
        Path(args.save).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
