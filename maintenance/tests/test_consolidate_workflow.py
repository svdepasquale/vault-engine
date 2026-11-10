"""consolidate.workflow.js under a stub Workflow runtime (node): a dead agent
(agent() -> null) must leave an error entry for its unit and keep the others."""
from __future__ import annotations
import json, shutil, subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "consolidate.workflow.js"
NODE = shutil.which("node")

# agent() returns the smallest object its schema requires, or null for the dead
# label; pipeline() runs the stages in order and drops an item that throws.
HARNESS = r"""
const [script, argsJson, dead] = process.argv.slice(1)
const src = require('fs').readFileSync(script, 'utf8').replace(/^export const meta/m, 'const meta')
const fill = (s) => Object.fromEntries((s.required || []).map(k => {
  const t = s.properties[k].type
  if (k === 'findings') return [k, [{ kind: 'LOST', page: 'p', why: 'w', fix: 'f' }]]
  return [k, t === 'array' ? [] : t === 'integer' ? 0 : `${k}`]
}))
const agent = async (p, o) => (o.label === dead ? null : fill(o.schema))
const pipeline = (items, ...stages) => Promise.all(items.map(async (it, i) => {
  let prev = it
  for (const st of stages) { try { prev = await st(prev, it, i) } catch (e) { return null } }
  return prev
}))
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
new AsyncFunction('agent', 'pipeline', 'phase', 'log', 'args', src)(
  agent, pipeline, () => {}, () => {}, JSON.parse(argsJson))
  .then(r => console.log(JSON.stringify(r)))
"""

ARGS = {"vault": "/v", "base": "b", "out": "/o", "date": "2026-01-01",
        "units": [{"id": "u1", "pages": [{"path": "wiki/a.md", "kb": 20}], "notes": ""},
                  {"id": "u2", "pages": [{"path": "wiki/b.md", "kb": 30}], "notes": ""}]}


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("stage", ["draft", "verify", "revise"])
def test_dead_agent_keeps_other_units_and_reports_its_own(stage):
    r = subprocess.run([NODE, "-e", HARNESS, str(SCRIPT), json.dumps(ARGS), f"{stage}:u1"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert [x["unit"] for x in out] == ["u2", "u1"]
    assert "error" not in out[0] and out[0]["findings"] == 1
    assert out[1] == {"unit": "u1", "error": stage}
