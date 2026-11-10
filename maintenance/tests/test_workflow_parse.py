"""Both Workflow scripts must at least parse: a stray backtick inside a
template literal kills the whole script at load, and nothing else loads it."""
from __future__ import annotations
import shutil, subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parent.parent).glob("*.workflow.js"))
NODE = shutil.which("node")

# The runtime runs the body as an async function with `export` removed; do the
# same and let node compile it (never run it).
CHECK = """
const src = require('fs').readFileSync(process.argv[1], 'utf8')
  .replace(/^export\\s+(?=const|let|function|async)/gm, '');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction(src);
"""


def test_scripts_found():
    assert len(SCRIPTS) >= 2


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_workflow_script_parses(script):
    r = subprocess.run([NODE, "-e", CHECK, str(script)],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_check_catches_unescaped_backtick(tmp_path):
    bad = tmp_path / "bad.workflow.js"
    bad.write_text("export const meta = {}\nconst x = `a `b` c`\n")
    r = subprocess.run([NODE, "-e", CHECK, str(bad)], capture_output=True, text=True, check=False)
    assert r.returncode != 0 and "SyntaxError" in r.stderr
