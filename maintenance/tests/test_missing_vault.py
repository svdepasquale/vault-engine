"""A mistyped WIKI_VAULT must fail loudly and create nothing: no script may
start a second, empty vault where the real one is not."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
ALLOCATE = ["bash", str(SCRIPTS / "allocate-address.sh")]


def run(cmd, vault):
    env = {**os.environ, "WIKI_VAULT": str(vault)}
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


# (command, exit code, whether its message names WIKI_VAULT)
COMMANDS = {
    "allocate": (ALLOCATE, 4, True),
    "allocate --peek": (ALLOCATE + ["--peek"], 4, True),
    "contextual-prefix --all": ([sys.executable, str(SCRIPTS / "contextual-prefix.py"), "--all"], 5, True),
    "contextual-prefix --peek": ([sys.executable, str(SCRIPTS / "contextual-prefix.py"), "--peek"], 5, True),
    "bm25 build": ([sys.executable, str(SCRIPTS / "bm25-index.py"), "build"], 4, True),
    "retrieve": ([sys.executable, str(SCRIPTS / "retrieve.py"), "anything"], 10, False),
}


@pytest.mark.parametrize("where", ["absent", "not-a-vault"])
@pytest.mark.parametrize("name", list(COMMANDS))
def test_a_wrong_vault_path_fails_and_creates_nothing(tmp_path, name, where):
    cmd, code, names_env = COMMANDS[name]
    if name.startswith("allocate") and not shutil.which("flock"):
        pytest.skip("allocate-address.sh needs flock")
    vault = tmp_path / "vault"
    if where == "not-a-vault":
        vault.mkdir()                     # an existing folder with no wiki/ in it
    out = run(cmd, vault)
    assert out.returncode == code, out.stderr
    if names_env:
        assert "WIKI_VAULT" in out.stderr
    if where == "absent":
        assert not vault.exists()
    else:
        assert list(vault.iterdir()) == []


def test_allocation_still_starts_at_one_in_a_new_vault(tmp_path):
    if not shutil.which("flock"):
        pytest.skip("allocate-address.sh needs flock")
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    out = run(ALLOCATE, vault)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "c-000001"
    assert (vault / ".vault-meta" / "address-counter.txt").read_text().strip() == "2"
