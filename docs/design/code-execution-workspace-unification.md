# Code-Execution / AI-Worker Workspace Unification

## Overview

We currently run two execution sandboxes that solve different problems but
duplicate a lot of mechanics:

- **`code-execution`** (kube-config: `containers/code-execution/`,
  `manifests/workloads/code-execution/`) — long-lived FastAPI pod with HTTP +
  MCP-over-SSE surfaces. Wired into FA's trusted profile as the `code-execution`
  MCP server. Workspaces are UUID dirs under a 1Gi `emptyDir`. Used for fast,
  inline "run this snippet" tool calls inside the LLM loop.
- **`ai-worker`** (FA: `services/backends/kubernetes.py`, kube-config:
  `containers/ai-coding-base/`) — Job-per-task spawn of an autonomous Claude
  Code or Gemini CLI agent. Workspaces are subdirs of a Longhorn RWX PVC
  (`tasks/<task_id>`), persistent across the Job's lifetime. Used for
  multi-turn, minutes-long agent work via `spawn_worker`.

The two systems' workspaces **do not overlap**: anything `code-execution`
writes is invisible to a worker the LLM spawns next, and vice versa. This
forecloses a useful workflow — "shell around interactively in code-exec, hand
off to a coding agent, then resume in the same shell."

This document plans the work to unify the workspace substrate so a single
workspace ID is addressable by either service, while keeping the two
services' execution shapes (long-lived shared pod vs. Job-per-task) intact.

> **Scope note.** This is *not* a plan to merge the services or unify their
> container images. Inline tool calls and autonomous agent runs have very
> different latency budgets; collapsing them is a maintenance trap. We are
> unifying the *workspace*, not the *executor*.

## Current State (Recap)

| Dimension          | code-execution                       | ai-worker                                |
| ------------------ | ------------------------------------ | ---------------------------------------- |
| Trigger            | MCP tool call inside LLM loop        | Async `spawn_worker`, webhook on done    |
| Lifecycle          | One shared pod, many callers         | One gVisor Job per task                  |
| Filesystem         | `emptyDir` (1Gi), `/tmp/workspaces`  | Longhorn RWX PVC, `/workspace/{shared,tasks/<id>}` |
| Workspace identity | UUID dir, addressed by absolute path | `task_id` chosen by FA, mounted via subPath |
| Image              | `python:3.12-slim` + media tools     | `node:24-slim` + claude-code/gemini/gh   |
| Egress             | Public 80/443 only                   | Public 443 + FA Service for webhook      |
| UID/GID            | 1000 / 1000                          | 1001 / 1001 (matches FA pod)             |
| Python execution   | `exec()` **in the FastAPI process**  | n/a (the agent decides)                  |

The seam:

1. code-exec passes `cwd` as an arbitrary absolute path through the API. FA
   (the LLM) holds onto that string and passes it back on subsequent calls.
2. ai-worker's `task_id` lives in a different namespace, on a different volume,
   under a different UID. There is no way to refer to one from the other.
3. Phase-1 work (separate doc/PR) hardens code-exec by moving Python out of
   in-process `exec()` and dedup'ing `main.py`/`mcp_server.py`. **That work is
   a prerequisite for this plan**, because once code-exec writes to FA's PVC,
   an in-process `exec()` bug would let one tool call corrupt another tenant's
   workspace.

## Goals

1. A single, durable concept of "workspace" that both services address by ID.
2. `code-execution` workspaces persist across pod restarts.
3. `spawn_worker` can target an existing `code-execution` workspace; the
   worker's `/task` mount and the LLM's next `code-execution` call see the
   same files.
4. No regression in the inline-tool latency profile (sub-second per call).
5. UID/GID alignment so all three pods (FA, code-exec, worker) write
   compatible files.

## Non-Goals

- Replacing in-loop code-execution with Job-per-call.
- Unifying the `code-execution` and `ai-coding-base` container images.
- Per-call gVisor isolation inside code-execution. Pod-level isolation +
  per-call subprocess (Phase 1) is sufficient for the inline-tool threat
  model.
- Multi-tenant workspace ACLs. Workspaces are owned by the FA instance; this
  is not a hosted product.
- Workspace versioning, snapshots, or quotas (left as follow-ups).
- Back-compat shims for the LLM-facing API. LLM tools are called on demand;
  the next call after rollout uses the new shape. We just change the API.

## Design

### Core Concept: Workspace as a First-Class Object

A **workspace** is a directory on the shared `family-assistant-workspace`
PVC, owned by FA, addressable by both services via a stable ID.

```
Workspace {
  id:           UUID (URL-safe string, e.g. "ws-7f3e8c…")
  path:         absolute path inside the pod's mount, e.g.
                /workspace/workspaces/ws-7f3e8c…
  created_at:   timestamp
  last_used_at: timestamp (touched on every code-exec call and worker spawn)
  owner:        opaque string (FA conversation_id by default)
}
```

Workspaces have no structured metadata file by default — `last_used_at` is
the directory's `mtime`. If we need richer metadata later we add a
`.workspace.json` marker; for now, simplicity wins.

### Storage Layout

The shared PVC currently has:

```
/workspace/
├── shared/                      # RO context for workers (existing)
└── tasks/                       # ai-worker per-Job dirs (existing)
    └── <task_id>/
```

After unification:

```
/workspace/
├── shared/                      # unchanged: RO context
└── workspaces/                  # the unified workspace substrate
    └── ws-<uuid>/               # addressable from code-exec AND ai-worker
```

`tasks/` goes away. `spawn_worker` always operates on a workspace under
`workspaces/`. Any leftover `tasks/<old_task_id>` dirs from before rollout
get reaped by the cleanup CronJob (below); we don't write code that
references them.

### code-execution Changes

#### Storage

- Replace the `emptyDir` mount with a subPath mount of
  `family-assistant-workspace`:
  ```yaml
  volumeMounts:
    - name: workspace
      mountPath: /workspace/workspaces
      subPath: workspaces
    - name: shared
      mountPath: /workspace/shared
      subPath: shared
      readOnly: true
  ```
- `WORKSPACE_BASE` env var moves from `/tmp/workspaces` to
  `/workspace/workspaces`.
- Remove the `tmp` `emptyDir` volume; if `/tmp` is needed for tool scratch,
  add a small dedicated emptyDir (256Mi).

#### UID/GID

- Change `runAsUser`/`runAsGroup`/`fsGroup` from `1000` to `1001` to match
  FA and ai-worker. Files created by code-exec are then group-readable by
  ai-worker without permission games.
- Update Dockerfile: `useradd -u 1001 -g 1001 codeexec`.
- `readOnlyRootFilesystem: true` stays. The PVC mount is RW; root FS is RO.

#### API Surface

The current API takes `cwd` as a free-form absolute path. The new API makes
workspaces first-class:

| Endpoint                                  | Purpose                                     |
| ----------------------------------------- | ------------------------------------------- |
| `POST   /workspaces`                      | Create — returns `{id, path}`               |
| `GET    /workspaces/{id}`                 | Metadata                                    |
| `DELETE /workspaces/{id}`                 | Remove                                      |
| `POST   /workspaces/{id}/execute`         | Run shell or Python                         |
| `POST   /workspaces/{id}/files`           | Write file                                  |
| `GET    /workspaces/{id}/files/{path}`    | Read file                                   |
| `GET    /workspaces/{id}/files`           | List files                                  |

The old `cwd`-style endpoints are removed.

#### MCP Tools

The `code-execution` MCP server tool set:

- `create_workspace()` → `{id, path}`
- `execute_shell(command, workspace_id?, timeout?)`
- `execute_python(code, workspace_id?, timeout?)`
- `write_file(path, content, workspace_id?)`
- `read_file(path, workspace_id)`
- `list_files(workspace_id, glob?)`
- `delete_workspace(workspace_id)`

`workspace_id` is **optional on the state-creating tools** (`execute_*`,
`write_file`). If omitted, the server mints a fresh workspace, runs the
operation, and returns the workspace ID in the response — keeping
single-shot snippets to one round-trip. The LLM can either ignore the ID
(letting the cleanup CronJob reap the dir) or reuse it for follow-up calls.

`workspace_id` is **required on the read/manage tools** (`read_file`,
`list_files`, `delete_workspace`) — there is nothing to read or manage in
an unspecified workspace.

`create_workspace` stays useful when the LLM wants to mint an ID up front
(e.g. to populate via several `write_file` calls, or to hand off to
`spawn_worker` before any code-exec activity).

The HTTP API stays explicit (`POST /workspaces/{id}/execute` etc.) — the
MCP server handles the anonymous case by minting a workspace in-process
before delegating. One HTTP shape; convenience lives at the LLM-facing
boundary.

### ai-worker Changes

#### `WorkerBackend.spawn_task`

Add a `workspace_id: str | None = None` parameter to the `WorkerBackend`
Protocol and all three implementations (`KubernetesBackend`,
`DockerBackend`, `MockBackend`).

```python
async def spawn_task(
    self,
    task_id: str,
    prompt_path: str,
    output_dir: str,
    webhook_url: str,
    model: str,
    timeout_minutes: int,
    context_paths: list[str] | None = None,
    callback_token: str | None = None,
    workspace_id: str | None = None,
) -> str: ...
```

Behavior:

- If `workspace_id` is given → Job mounts subPath `workspaces/<workspace_id>`
  at `/task`. The worker sees prompt + any pre-existing files; its outputs
  land in the same dir code-exec is using.
- If `workspace_id` is `None` → FA mints a fresh workspace ID, creates
  `workspaces/<id>/` on the PVC, and spawns the worker against it. The
  workspace ID is included in the `spawn_worker` tool result so the LLM can
  follow up.

`workspace_id` is validated: reject values containing `/`, `..`, or
non-URL-safe characters. The string is interpolated into the Job manifest.

#### FA `spawn_worker` Tool

Tool signature gains a `workspace_id` parameter (optional). Returns
`{task_id, workspace_id}` so the LLM always knows where the worker's
output lives, whether it brought its own workspace or got a fresh one.

Updated tool description (gist):

> `spawn_worker` runs an autonomous coding agent in an isolated container.
> It always operates inside a workspace on the shared filesystem.
>
> - Pass `workspace_id` to continue work in an existing workspace (e.g.
>   one you created with `create_workspace` and pre-populated using
>   code-execution tools). The worker can read everything already there;
>   files it writes will be visible to subsequent code-execution calls.
> - Omit `workspace_id` to get a fresh, isolated workspace. The
>   workspace ID is returned so you can read the worker's output later.

`read_task_result` is unchanged. `run-task` continues to write structured
output to `$TASK_OUTPUT_DIR` (`/task/output/result.json`); only the subPath
of the mount changes.

### Permissions & Security

- **Same UID across all three pods** (1001). Workspace files end up
  uid=gid=1001, group-writable. No `chmod` dance.
- **Subpath isolation**. code-exec mounts only `workspaces/` (RW) and
  `shared/` (RO). Workers continue to mount only their own workspace +
  `shared/` RO. FA itself is the only pod with the whole PVC.
- **NetworkPolicy unchanged**. code-exec still public-only egress; workers
  still public + FA-Service.
- **`readOnlyRootFilesystem: true`** on code-exec stays.
- **Blast radius.** A code-exec compromise can now write to FA's PVC under
  `workspaces/`. Today it can already DoS or exfiltrate via the public
  egress allowance. The marginal new capability is "corrupt another LLM
  session's workspace" — material, but bounded. The Phase-1 subprocess move
  closes the within-process leak path; subPath isolation closes the
  out-of-substrate path. We accept the residual risk.
- **Quotas.** Out of scope. Document as a follow-up. Default emptyDir
  `sizeLimit: 1Gi` was barely a quota; the PVC is sized for FA. Soft
  control via TTL (below).

### Lifecycle & Cleanup

`emptyDir` gave us free cleanup on pod restart; the PVC does not. We need a
cleanup story:

- Each API call that touches a workspace updates its directory `mtime`
  (`os.utime` on the workspace dir). `last_used_at = mtime`.
- A new `code-execution-cleanup` Kubernetes `CronJob` (daily) walks
  `workspaces/` and removes any directory with `mtime` older than 7 days.
  Configurable via env var.
- Manual `DELETE /workspaces/{id}` remains the immediate-cleanup path.
- Workspaces with an active worker Job are protected: the cleanup CronJob
  skips any directory referenced by a non-terminal Job (label selector
  `app=ai-worker` + look at the Job's mounted subPath).
- Cleanup CronJob lives alongside the existing ai-worker manifests in
  `kube-config/kubernetes/manifests/workloads/ai-worker/`, not in the
  code-execution image. Both services consume the same workspace substrate;
  keeping the lifecycle CronJob next to the worker plumbing keeps the
  related concerns together.

### LLM-Facing Tool Surface (after unification)

```
LLM tools (FA):
  spawn_worker(task_description, agent, context_paths, timeout_minutes,
               workspace_id?)
    → {task_id, workspace_id}
  read_task_result(task_id, ...)
  cancel_worker_task(task_id)

LLM tools (code-execution MCP server):
  create_workspace()            -> {id, path}
  execute_shell(command, workspace_id?, timeout?)        -> {..., workspace_id}
  execute_python(code, workspace_id?, timeout?)          -> {..., workspace_id}
  write_file(path, content, workspace_id?)               -> {..., workspace_id}
  read_file(path, workspace_id)
  list_files(workspace_id, glob?)
  delete_workspace(workspace_id)

  # `workspace_id` is optional on state-creating tools — server mints one
  # if absent and returns it in the response so the LLM can reuse it.
  # Required on read/manage tools.
```

The intended LLM ergonomic flow:

```
1. ws = create_workspace()
2. write_file("data.csv", <content>, ws.id)
3. execute_shell("head -n 5 data.csv", ws.id)
4. spawn_worker(task_description="…analyze data.csv and write report.md…",
                workspace_id=ws.id, timeout_minutes=15)
5. (worker runs; webhook fires; LLM is woken)
6. read_task_result(task_id)
7. read_file("report.md", ws.id)        # produced by the worker
8. execute_shell("wc -l report.md", ws.id)
9. delete_workspace(ws.id)              # or let TTL reap it
```

## Phasing

Each phase is independently shippable.

### Phase 1 (prereq, separate plan): Harden `code-execution`

Tracked separately. Required before Phase 2 lands because in-process
`exec()` becomes a cross-tenant concern once we share storage.

- Move Python execution to a per-call subprocess via the same path as shell.
- Collapse `main.py` and `mcp_server.py` into a single module that exports
  both transports.
- Acceptance: existing FA behaviors unchanged; no new isolation issues
  detectable by the test suite.

### Phase 2: Workspace as a noun, on the shared PVC

The visible change. Lands as one PR per repo (FA + kube-config), merged
together.

**code-execution (kube-config):**
- Replace `emptyDir` mount with `family-assistant-workspace` PVC subPath.
- Switch UID/GID/fsGroup to 1001.
- Implement the new `/workspaces/...` API; remove the `cwd`-style endpoints.
- Update MCP server tool definitions; `workspace_id` required everywhere.
- Add `code-execution-cleanup` CronJob workload.

**FA:**
- Update tool descriptions for the `code-execution` MCP server (the LLM
  needs to learn the new tool shapes).
- Update functional tests that exercise the `code-execution` tools.

Acceptance: a workspace created before pod restart is still readable
afterwards; FA's MCP integration tests pass against the new shape.

### Phase 3: `ai-worker` accepts `workspace_id`

- Extend `WorkerBackend` Protocol + the three backends.
- `KubernetesBackend._build_job_manifest`: subPath = `workspaces/<id>`,
  always. If `workspace_id` is None, FA mints one and `mkdir`s before
  spawning the Job. Drop the `tasks/<task_id>` path entirely.
- `spawn_worker` tool gains `workspace_id`; tool result includes
  `workspace_id`.
- Update FA functional tests covering the handoff flow.

Acceptance: spawn_worker against an existing code-exec workspace; worker
writes a file; subsequent `read_file` via code-execution sees it.

### Phase 4 (optional, follow-up): shared sandbox library

Extract the gVisor pod template, NetworkPolicy boilerplate, and path
validation into a shared chart or library so the two services don't drift
on security defaults. Low priority; only worth doing if Phase 3 reveals real
duplication pain.

## Risks & Open Questions

1. **PVC contention.** Longhorn RWX is fine for FA's current load.
   code-execution adds many small writes from the LLM tool loop. Mitigation:
   add a `workspace_write_seconds` histogram on code-execution before Phase
   2 ships, then watch it. Falls back to per-pod `emptyDir` for hot paths
   if latency degrades visibly.
2. **Cleanup correctness.** If the cleanup CronJob deletes a workspace
   that's about to be used, the LLM gets a confusing 404. Mitigation:
   cleanup checks for active Jobs; idle-for-7-days code-exec sessions are
   accepted as collateral and documented prominently in tool descriptions.
3. **Concurrent writers.** Two parallel LLM tool calls into the same
   workspace can race. Same is true today within a single workspace.
   Workspaces are documented as single-writer; no locking added.
4. **UID change disruption at Phase 2 cutover.** Existing emptyDir
   workspaces are 1000-owned and get wiped at pod restart anyway, so the
   UID change rides along.
5. **Tool description drift.** Two MCP servers (`code-execution`,
   `homeassistant`, etc.) all have descriptions the LLM uses for routing.
   Adding the workspace handoff changes how tools compose. Exercise this
   end-to-end in FA functional tests, not just unit tests.
6. **Quota enforcement.** A misbehaving LLM could fill the PVC via
   code-execution writes faster than TTL can reap. Future work:
   per-workspace `du` ceiling, or xfs project quotas.

## Appendix: Concrete End-to-End Example

User asks the assistant: *"Look at expenses.csv I just attached and figure
out what's blowing the budget — make a report and a chart."*

1. FA's trusted profile gets the message. The attachment is staged into
   FA's storage.
2. LLM calls `create_workspace` → `{ id: "ws-a1b2", path: "/workspace/workspaces/ws-a1b2" }`.
3. LLM calls `write_file("expenses.csv", <b64>, "ws-a1b2")`. Code-exec
   writes to `/workspace/workspaces/ws-a1b2/expenses.csv`.
4. LLM calls `execute_shell("head -n 5 expenses.csv && wc -l expenses.csv", "ws-a1b2")`
   to peek at the data.
5. The data is bigger / messier than a single tool call can chew on. LLM
   decides to delegate.
6. LLM calls `spawn_worker(task_description="Analyze expenses.csv in this workspace, write report.md and chart.png. Be thorough.", workspace_id="ws-a1b2", timeout_minutes=15, agent="claude")`.
7. FA's `KubernetesBackend.spawn_task` builds a Job manifest with
   `subPath: workspaces/ws-a1b2`. Job mounts that path at `/task`.
8. The worker pod runs Claude Code, which sees `/task/expenses.csv`,
   writes `/task/report.md`, `/task/chart.png`, and `/task/output/result.json`.
9. `run-task` POSTs the webhook to FA. FA wakes the LLM with the task
   completion event.
10. LLM calls `read_task_result(task_id)` → outcome=success.
11. LLM calls `read_file("report.md", "ws-a1b2")` and `read_file("chart.png", "ws-a1b2")`
    via code-execution — same files the worker just wrote.
12. LLM summarises to the user, attaches the chart, and (optionally) calls
    `delete_workspace("ws-a1b2")`. If it forgets, the cleanup CronJob reaps
    the dir after 7 idle days.

This flow is impossible today.
