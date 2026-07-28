# What `xahaud:` citations in this repo point at

Claims about xahaud's behaviour are written inline, in one form:

    xahaud:src/xrpld/rpc/handlers/ServerDefinitions.cpp:542

A repo-relative path, a line, and a prefix that makes them greppable:

    rg 'xahaud:' src/

They all refer to **one commit**:

| | |
|---|---|
| Repo | https://github.com/Xahau/xahaud |
| Ref | `origin/dev` |
| Commit | `bb244ef7729503a0317bcff0f8fdaa93ca5cb7d2` |
| Date | 2026-06-21 |

Browse one directly by substituting the path and line:

    https://github.com/Xahau/xahaud/blob/bb244ef7729503a0317bcff0f8fdaa93ca5cb7d2/<path>#L<line>

Or read it locally from any checkout containing the commit, without moving
that checkout off whatever branch it is on:

    git -C <xahaud> show bb244ef7:<path> | sed -n '<line>p'

## Why a pin rather than a branch

Line numbers move. A citation naming `dev` is true until someone edits the
file above it, and then it silently points at something else — which is worse
than no citation, because it still reads like one. Naming a commit means a
citation is either correct or checkably wrong.

Nothing here verifies these automatically; this is the same syntax hookz uses,
where `hookz.wasm.xahaud_ref.check_citations` does enforce it. If that becomes
worth having here, the citations are already in the form it parses.

## Re-pinning

Bump the commit above, then re-read every `xahaud:` line — the whole point is
that they were true at a known revision, so moving the revision without
re-checking them converts a set of facts into a set of guesses.
