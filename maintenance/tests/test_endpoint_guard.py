"""Pytest suite for scripts/endpoint-guard.py (no network: literals and a stubbed resolver)."""
from __future__ import annotations
import importlib.util, socket
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "endpoint-guard.py"


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.delenv("WIKI_ALLOW_PUBLIC_ENDPOINTS", raising=False)
    spec = importlib.util.spec_from_file_location("endpoint_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolves_to(monkeypatch, *addrs):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in addrs]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:1234",            # this machine
    "http://[::1]:8080/v1",
    "http://192.168.1.20:8080",         # LAN
    "http://10.0.0.5",
    "http://100.101.102.103:8080",      # tailnet (100.64.0.0/10)
    "http://169.254.10.1",              # link-local
])
def test_machines_you_control_pass(guard, url):
    assert guard.check(url) is None


@pytest.mark.parametrize("url", ["http://8.8.8.8:8080", "https://1.1.1.1/v1/chat/completions"])
def test_public_address_is_refused(guard, url):
    assert "public address" in guard.check(url)


def test_hostname_is_judged_by_every_address_it_resolves_to(guard, monkeypatch):
    resolves_to(monkeypatch, "100.70.1.2")
    assert guard.check("http://llm-box:8080") is None
    resolves_to(monkeypatch, "100.70.1.2", "203.0.114.9")   # one public address is enough to refuse
    assert "203.0.114.9" in guard.check("http://llm-box:8080")


def test_unresolvable_host_is_refused(guard, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(8, "nodename nor servname provided")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert "does not resolve" in guard.check("http://typo-box:8080")


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/x", "127.0.0.1:1234", "http://127.0.0.1:99999"])
def test_malformed_urls_are_refused(guard, url):
    assert guard.check(url) is not None


def test_opt_in_lets_a_public_host_through(guard, monkeypatch):
    assert guard.check("http://8.8.8.8", allow_public=True) is None
    monkeypatch.setenv("WIKI_ALLOW_PUBLIC_ENDPOINTS", "1")
    assert guard.check("http://8.8.8.8") is None


def test_cli_exit_codes(guard, capsys):
    assert guard.main(["http://127.0.0.1:1234"]) == 0
    assert guard.main(["http://8.8.8.8"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert guard.main([]) == 2
