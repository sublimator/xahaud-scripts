"""Inspect live Xahau/XRPL networks: amendment status and overlay version mix.

Public network introspection that needs no local node:

- ``amendments`` queries the public ``server_definitions`` RPC (xahaud returns
  the full amendment table to anonymous callers) to show what is enabled,
  vetoed, or pending — and diffs mainnet against testnet.
- ``crawl`` walks the peer overlay ``/crawl`` endpoint breadth-first and reports
  the distribution of software versions across unique nodes.
"""
