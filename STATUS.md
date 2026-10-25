# STATUS

## Now: one-click sign-in for the viewer (2026-10-24, owner: "how can I use something if I can't login")

Problem: the viewer needs a token, the owner token was shown once at `memctl init` and only its hash is kept, so the owner was locked out.

Plan (test first):
1. `memctl ui`: keep the owner's sign-in in `~/.hydra/memsvc/ui.token` (mode 600). If it is missing or no longer matches
   `admin.token`, make a fresh owner token; the old `admin.token` is moved aside to `admin.token.replaced-<time>`, never deleted.
   Then open `http://127.0.0.1:8765/#token=...` in the default browser. The token never goes to stdout.
2. `ui.html`: read `#token=` once, keep it in sessionStorage like a pasted token, and wipe it from the address with `history.replaceState`.
   The fragment is never sent to the server, so it is not in the access log.
3. `memctl doctor` checks `ui.token` is mode 600.
4. App-grid icon "Memory" that runs `memctl ui`.

Why this is safe enough: a token file readable only by the owner's account protects against what the token is for (web pages and other users).
Any program running as the owner can already read `~/.hydra/graph.db` directly.

Assumption: the owner holds no working copy of the current admin token (the one they pasted matches no stored hash), so replacing it loses nothing.

Done 2026-10-24 19:46: all four steps. 500 tests in 30 suites OK; 3 sign-in mutants killed; live check: signed-in
/v1/stats 200, viewer opened signed in (owner POST /v1/search 200), usage log recording; launcher ~/.local/share/applications/memory-viewer.desktop.
Open: publishing this code (it is not in git or on GitHub yet) waits on the owner's choice of where.

## BLOCKERS
(none)
