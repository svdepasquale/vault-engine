#!/usr/bin/env bash
# Run the wiki-maintenance pytest suite via uv.
# Self-contained: uv resolves pytest + pyyaml on first run, cached after.
set -euo pipefail

cd "$(dirname "$0")"
uv run --with pytest --with pyyaml pytest tests/ -v "$@"
