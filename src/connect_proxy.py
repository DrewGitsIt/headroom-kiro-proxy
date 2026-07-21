"""Backwards-compatibility shim — delegates to proxy.py.

The proxy logic has been split into:
  proxy.py        — connection handling, server lifecycle, tunneling
  interceptor.py  — TLS interception, request parsing, upstream forwarding
  stats.py        — stats tracking, cost estimation

This file exists so that existing install scripts and launchd plists that
reference ``python connect_proxy.py --port 9090`` continue to work.
"""

from proxy import main

if __name__ == "__main__":
    main()
