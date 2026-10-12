#!/usr/bin/env python3
"""endpoint-guard.py — keep vault text on machines you control.

Every script that POSTs vault content over HTTP checks its URL here first:
chunks to an embedding server (rerank.py, tiling-check.py), passage pairs to
the semantic-scan judge, page pairs to the dream model, lint findings to the
maintenance LLM (wiki-maintenance.sh, through the CLI below).

A URL passes when every address its host resolves to is non-global:
loopback, RFC 1918 LAN, the 100.64.0.0/10 shared range a tailnet hands out,
link-local and IPv6 ULA. So a model server on this machine, the LAN or the
tailnet works with no flag, and a public address — a cloud API, a typo that
happens to resolve — is refused. A host that does not resolve is refused
too: the guard does not guess where it would point. Opt in to public hosts
with WIKI_ALLOW_PUBLIC_ENDPOINTS=1, or with the calling script's own flag
(rerank.py and tiling-check.py keep --allow-remote-backend).

  endpoint-guard.py URL    exit 0 when URL may receive vault text, else 1
                           with the reason on stderr (for shell callers)
"""
import ipaddress
import os
import socket
import sys
import urllib.parse

OPT_IN_ENV = "WIKI_ALLOW_PUBLIC_ENDPOINTS"


def check(url, allow_public=False):
    """None when `url` may receive vault text, else the reason it may not."""
    if allow_public or os.environ.get(OPT_IN_ENV) == "1":
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        parsed.port  # raises ValueError on a malformed port
    except ValueError as e:
        return f"{url!r} is not a valid URL ({e})"
    if parsed.scheme not in ("http", "https") or not host:
        return f"{url!r} is not an http(s) URL with a host"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        return f"host {host!r} does not resolve ({e}), so where it points cannot be checked"
    public = sorted({info[4][0] for info in infos
                     if ipaddress.ip_address(info[4][0].split("%")[0]).is_global})
    if public:
        return (f"host {host!r} resolves to a public address ({', '.join(public)}): "
                f"vault text would leave this machine, its LAN and its tailnet. "
                f"Set {OPT_IN_ENV}=1 if that is intended")
    return None


def main(argv):
    if len(argv) != 1:
        print("usage: endpoint-guard.py URL", file=sys.stderr)
        return 2
    why = check(argv[0])
    if why:
        print(f"endpoint-guard: refusing {argv[0]}: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
