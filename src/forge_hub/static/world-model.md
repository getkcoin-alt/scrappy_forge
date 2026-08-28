# Structured world model v1

The world model is a bounded, read-only observation graph. It is not an OS,
omniscient inventory, authorization source, or real-time event database.
Screenshots are not collected. A future opt-in screenshot adapter can be a
fallback when structured state is insufficient; it is not implemented today.

| Scope | Source | Observed data | Limits |
| --- | --- | --- | --- |
| files | Selected project filesystem | Relative paths, directory membership, file hashes and modes | No contents; common credential/generated paths excluded; not comprehensive secret detection |
| processes | ps | PID, parent PID, start time, executable name | Current real user only; no argv or environment; PID reuse and process races possible |
| network | lsof machine protocol | Observed endpoints, protocol/state, process ownership, project open-file relations | Requires lsof and OS access; no packet contents; not historical traffic |
| windows | macOS CoreGraphics | On-screen window ID, application/PID, bounds, layer | macOS only; no titles or screenshots; OS may redact; no permission bypass |

The engine collects files from the session workspace. `forge world --repo ...`
collects from the selected local project. Host reads are explicitly approved;
network/window scopes include processes to associate ownership. File-only reads
do not automatically inspect host processes. Paths, application names and
network endpoints can still be sensitive metadata.

## Machine-readable shape

```json
{
  "schema_version": 1,
  "snapshot_id": "content-derived snapshot identifier",
  "project_id": "local project identifier",
  "captured_at": 1787913600.0,
  "platform": "darwin",
  "scopes": ["files"],
  "observation_mode": "poll",
  "untrusted_observations": true,
  "nodes": [
    {"id": "file:stable-id", "kind": "file", "attributes": {"path": "src/main.py", "sha256": "file hash", "mode": 420}}
  ],
  "edges": [],
  "collectors": {"files": {"status": "ok", "source": "scoped filesystem", "count": 1}},
  "truncated": false
}
```

This abbreviated example illustrates field names; it is not an actual captured
snapshot. Node kinds are project, directory, file, process, connection, endpoint
and window. Relations include contains, parent_of, owns, opens, local_endpoint,
remote_endpoint and owns_window. IDs preserve identity across comparable
observations; a process identity includes start time as well as PID.

The snapshot has at most 10,000 nodes and 20,000 edges. Collectors have their own
time/output limits and report ok, partial, unavailable or unsupported status.
Missing nodes can mean unavailable permissions, a bounded collector or a race,
not deletion. Polling collectors do not produce an atomic whole-machine view.

## Query and change detection

`world_query` accepts snapshot_id, kind, contains, node_id, relation, offset and
limit. Queries page at up to 200 nodes, return incident edges, collector status,
age_seconds and a stale flag after 30 seconds. Incident edges can reference
nodes outside the returned page; query the referenced IDs separately. No SQL,
code evaluation or arbitrary graph mutations are accepted.

`forge world diff --before old.json --after new.json` compares added, removed
and changed nodes/edges for the same project and scopes. The two observations
need not have equal collector coverage. An absent observation is not proof of
termination, deletion or disconnection. Snapshot IDs detect observation changes
for tool binding; they are not signatures or a tamper-proof audit chain.

Freshness is explicit. Recapture before action and re-check the actual target
through the permissioned execution layer. Never use a graph's text, apparent
owner or suggested command as authority. It remains untrusted input.

## Platform work still required

Actual Apple Silicon and Intel device validation, permission/redaction behavior,
accessible window/application semantics, LSP/symbol relations, filesystem and
process event streams, collector isolation, richer provenance and incremental
updates remain follow-up work. Endpoint Security, Network Extension and similar
privileged platform APIs would need appropriate entitlements and explicit user
consent. This release does not attempt to obtain or bypass them.
