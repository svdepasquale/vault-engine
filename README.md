# vault-engine

Retrieval, maintenance and evaluation tooling for a plain-markdown knowledge vault that serves as Claude Code's long-term memory.

- **Retrieval** — paragraph chunks with a synthetic page prefix, a BM25 index, and a local embedding rerank (Homebrew `llama-embedding` with qwen3-embedding-0.6B, nothing resident; several embedders listed in `WIKI_EMBED_MODEL` are fused by per-query z-scores); chunk-level output sized for an LLM's context.
- **Maintenance** — deterministic lint (links, frontmatter, typed-relation predicates and inverses; a `--brief` form for a session-start hook, `--only` for the pages a session just changed), a frontmatter check that YAML reads each page the way the line-based readers do, and autofix, local-LLM fix proposals behind a judge, a semantic scan for contradictions and duplicates across pages, a consolidation signal for pages that have grown into journals, and an ideation ("dream") pass.
- **Evaluation** — a gold-set harness (page recall, MRR, and knowledge-update scoring: does the *current* fact win over a stale one).

The vault it works on is a separate repository, set with `WIKI_VAULT`. See `CLAUDE.md` for the layout and the contract.

## Use

Clone this repo next to the vault (`~/projects/vault-engine` and `~/projects/knowledge-vault` by default) or point `WIKI_VAULT` at the vault. Typical calls:

```sh
python3 scripts/contextual-prefix.py --all && python3 scripts/bm25-index.py build   # (re)build the index
python3 scripts/retrieve.py "your question" --top 3 --chunks --compact               # hybrid retrieval
python3 eval/run-eval.py --gold goldset.json                                          # gate against the vault's gold set
bash maintenance/run-tests.sh                                                         # tests (pytest via uv)
```

Embeddings run locally through Homebrew `llama-embedding` on GGUF models; without it, retrieval degrades to BM25 order and says so in its output. Any script that sends vault text over HTTP (an embedding server, the local judge and dream models) accepts only hosts on this machine, the LAN or a tailnet; a public address needs `WIKI_ALLOW_PUBLIC_ENDPOINTS=1`.

## Credits

The retrieval and memory scripts started as a fork of [claude-obsidian](https://github.com/AgriciDaniel/claude-obsidian) (MIT) and have been patched and extended since; see `LICENSE`.
