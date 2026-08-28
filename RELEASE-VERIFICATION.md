# 0.4 release verification

Local verification on 28 August 2026, Linux x86_64, Python 3.12.13:

| Check | Result |
| --- | --- |
| Ruff lint and format | Passed |
| Automated tests | 148 passed |
| Built wheel installed in isolated uv tool environment | Passed; imported installed package, not editable source |
| Repeated identical installation | Passed |
| Real PTY masked key setup, 0600 save and key removal | Passed, using an explicit non-secret fixture |
| Installed CLI file-graph snapshot/query | Passed |
| Installed CLI edit, failed baseline, passing final check, reviewed apply | Passed with real subprocesses and a scripted model |
| MCP OAuth/PKCE callback and authenticated local tool | Passed with a local issuer; browser redirect simulated |
| Actual localhost release server, readiness, wheel checksum, docs and installer syntax | Passed |
| Live OpenRouter inference | Not run; no model API key used |
| Live Supabase account flow | Not run; account service unconfigured |
| Hosted GitHub macOS runner | Tests and real PTY workflow passed on the v0.4 implementation commit; not a consumer-device compatibility matrix |
| Linux GitHub Python 3.11/3.12/3.13 | Tests, lint and terminal workflow passed |
| Local Docker build | Not run; Docker unavailable locally |
| Railway container build and internal /readyz health check | Passed on the v0.4 implementation commit |
| Independent security audit or 10x productivity benchmark | Not performed |

During installed-terminal testing, TERM=dumb exposed a password-rendering
shortcut in the prompt library. The secret prompt now uses the regular masked
renderer explicitly and a real-PTY regression test covers the minimal terminal.

Run `python validation/release_smoke.py` to repeat wheel/install/runtime checks.
Its output identifies the isolated installation and temporary evidence paths.
Generated local transcripts/fixtures are intentionally excluded from Git.

GitHub CI and Railway deployment outcomes must be checked independently against
the release commit. A workflow definition is not a passing run; a public login
page does not imply that account registration has been activated.

The first Docker CI readiness request raced container startup and received a
connection reset. The bounded readiness retry now includes connection resets;
it still fails if readiness never succeeds. This check must pass on the follow-up
commit before the complete workflow can be considered green.
