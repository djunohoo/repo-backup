# Fleet Backup — build spec v1

**For:** the Claude Desktop session with weekly budget that is already building GitHub repo watching for the admin deck.
**From:** code chair, PIX3L-SHIFT3R, 2026-09-13.
**Status:** design agreed with Keenan. Nothing in "To build" is started.

---

## 1. Why this exists, in one paragraph

56 repos are bundled nightly to Dropbox by a single hardcoded script on one
machine. It works, but it is a static path list on one host, it re-uploads
5.27 GB every night because every bundle is a new filename, it has no idea
whether a bundle can actually be restored, and nothing anywhere reports on it.
The fleet is growing. This spec turns that one script into a per-machine agent
with central reporting, and — the part that actually matters — makes the system
tell the truth about what is and is not recoverable.

---

## 2. What is already true (done 2026-09-13, do not redo)

**Dropbox race fixed.** `git bundle create` writing straight into a Dropbox
folder loses a race with Dropbox's filter driver, which grabs newly created
files (including the `<name>.bundle.lock` git writes before renaming) and fails
the open with `Permission denied`. Measured: the identical command succeeded
1 time in 5. This silently failed 6–8 repos every night from 2026-08-26 to
2026-09-13 — 288 failures, all reported honestly in the log, none acted on.
Fix: bundle and verify on local disk (`staging_dir`), then `shutil.move` into
Dropbox with bounded retry. Result went from `ok=50/56` to `ok=56/56`, and the
run got ~2.3× faster (90 s vs 3.5 min) because 5 GB no longer streams through
Dropbox's filter driver.

**Layout reorganized and migrated.** See §4. 392 bundles moved with byte-for-byte
verification; the old `Organized\DEV\Backups\repos` tree is gone.

**Hostname is derived, not configured.** `HOSTNAME = socket.gethostname().upper()`
in `backup_repos.py`. The same `config.json` is now correct on every machine.
This matters: many repo names exist on more than one host, and a hand-edited
hostname copied with the config would silently overwrite another machine's
bundles.

**Code:** `D:\DEV\TOOLS\repo-backup\backup_repos.py` + `config.json`.
Scheduled task `RepoBackup-Daily`, 03:00 local, runs as `djunohoo`.

---

## 3. What already exists — reuse, do not rebuild

This is the most important section. Three of the four capabilities requested
already exist somewhere in the fleet.

### 3.1 Repo discovery — ALREADY BUILT
`D:\DEV\PROJECTS\TOOLS_Custom_MCPs\fleet-git-dashboard\`

- `reporter/fleet_git_reporter/collect.py` — walks `FLEET_GIT_REPO_ROOTS` for
  `.git` dirs, also finds Claude Code worktrees under `~/.claude/projects` and
  `.claude-worktrees/`. Runs `git fetch` with `GIT_TERMINAL_PROMPT=0` and
  `GIT_ASKPASS=echo` so it cannot hang on a credential prompt. Collects branch,
  commit SHA, **ahead/behind counts**, mtime. Reports `fetch_ok` + `fetch_error`
  rather than passing stale data off as fresh.
- `reporter/fleet_git_reporter/report.py` — upserts a snapshot scoped to
  `user_id + hostname + snapshot_at`, prunes prior snapshots, and guards the
  prune when a scan returns zero repos.

**This replaces the static 56-path list in `config.json`.** Do not write a new
scanner.

**Blocker:** the exact Supabase table it writes to could not be determined from
the code — `report.py` takes the table name as a parameter. Resolve this first;
there is already a board note saying the fleet-git-dashboard write target is
unknown and that work should not be stacked on it until it is.

### 3.2 Per-machine agent + scheduling — ALREADY BUILT
`D:\DEV\PROJECTS\TOOLS_Custom_MCPs\cortex-task-runner\`

Registers the host in `fleet_nodes`, heartbeats hourly with CPU/RAM/disk/GPU,
polls `claim_next_task(hostname, lease_seconds)`, leases, runs, streams
`task_messages`, enforces runtime and token budgets, releases.

**Design decision — read this before wiring backup into it.** The task runner
executes each claimed task by spawning a **headless Claude SDK session**. That is
the right shape for "go implement this ticket" and the wrong shape for a nightly
backup. Backup is deterministic, runs 365 nights a year, and must behave
identically every time. Putting an LLM in that loop costs API credits nightly and
introduces non-determinism into the one system whose entire job is to be
trustworthy.

**Therefore: the backup agent is plain deterministic code.** It may reuse the
runner's registration and heartbeat plumbing, and the runner may be used to
*schedule* or *trigger* it, but the bundling, validation, and retention logic
must never run inside an LLM session. (An earlier research pass recommended
making it a task type; that recommendation is explicitly overridden here.)

### 3.3 GitHub watching — PARTIALLY BUILT (this is your half)
Supabase project `anbcpdcririfpnxsvwfb` (cortex17-docjock-v2), `deck_` prefix.

- `deck_github_events` — migration `009_github_events.sql`. Columns: org, repo,
  entity_type (pr/issue/branch/commit), entity_id, state, author, `raw_payload`
  JSONB, is_stale, first_seen_at, last_updated_at, synced_at. Unique on
  (repo, entity_type, entity_id) for idempotent upsert.
- `deck_user_github_tokens` — migration `020_github_integration.sql`. Stores
  per-user GitHub access tokens **in plaintext**, and is deliberately not exposed
  by any MCP tool.
- MCP tools `list_github_events` / `update_github_event` on deck-board-mcp.
- Population is done by **n8n, off-box**. No polling loop exists in the repo.

### 3.4 The admin deck itself
`D:\DEV\PROJECTS\cortex-board-widget-style2\` (the older `cortex-board\` is v1).
Node/Express + React/Vite + Supabase.

**There is no widget registry and no `widgets.json`.** "Widgets" are hard-coded
React pages in `src/pages/` (BrainstormPage, DailyLogPage, HubPage, InboxPage,
ProjectDetailPage, RoadmapPage, TimelinePage, WeeklyReviewPage, WellnessPage),
wired into `App.tsx` routing. Adding one = new `.tsx` + a route + rebuild. Plan
for that, not for a plugin system.

---

## 4. Canonical layout (implemented, migrated, live)

```
S:\Aperture Eclipse Dropbox\Backups\
├── README.md
├── fleet\<HOSTNAME>\
│   ├── repos\<repo>\full\   verified full bundles      [LIVE — 392 files, 34.4 GB]
│   ├── repos\<repo>\incr\   incremental bundles        [to build]
│   └── manifests\           run manifests + ledger     [to build]
├── apps\<app>\              opaque app-state snapshots
├── media\                   personal media dumped here
└── misc\                    one-offs
```

Rules: host-scoped under `fleet/`; `full/` and `incr/` are siblings so retention
counts only fulls; each `apps/<app>/` entry is an opaque object that is moved
whole and never reorganized internally.

---

## 5. To build

### 5.1 Incremental + periodic full (Keenan's choice)

Weekly full, nightly incremental.

- **Full:** `git bundle create full.bundle --all`, then record every ref→SHA into
  a manifest JSON alongside it.
- **Incremental:** `git bundle create incr.bundle --all --not <every SHA from the
  last full's manifest>`. Nightly deltas drop from gigabytes to kilobytes for
  repos that did not change.
- **Restore:** `git clone full.bundle`, then apply each incremental **in order**
  via `git fetch <incr.bundle> 'refs/*:refs/*'`.

**The hazard, stated plainly.** A chain is only as good as its weakest link, and
`git bundle verify` on an incremental only proves its prerequisites are
satisfiable *against a repo that already has them*. Verifying an incremental in
isolation proves close to nothing. This is precisely the failure silent-failure-guard
exists to prevent: a system that reports seven green backups while six of them
are unrestorable. Hence §5.2 is not optional — it is the feature.

### 5.2 Validation = a real restore, not `bundle verify`

A scheduled validator that, per repo on a rotation:

1. Clones the newest full into a temp dir.
2. Replays every retained incremental in order.
3. Compares resulting ref→SHA against the manifest recorded at capture time.
4. Writes the outcome to the ledger with `verify_method='restore_test'`.

`bundle verify` stays as the cheap per-write gate. `restore_test` is the only
thing allowed to mark a chain trustworthy. **Never report a backup as good on the
strength of `bundle verify` alone.**

### 5.3 Retention with a keep-known-good floor

Keenan's rule: delete older than X, **but never delete the last one verified to
work.** Concretely, a bundle is prunable only if all hold:

- older than the retention window, **and**
- not the newest full with `restore_test` = pass, **and**
- not the basis of any retained incremental, **and**
- at least one *newer* full has passed a `restore_test`.

That last clause is the safety interlock. If validation has been failing for
three weeks, retention must **stall and raise**, not quietly eat the last good
copy. Prune only after a success, never on a schedule alone.

### 5.4 Discovery replaces the static list

Agent calls fleet-git-dashboard's collector (§3.1) instead of reading a repo
array. `config.json` keeps only roots, excludes, retention, staging, destination.

### 5.5 Data model

Supabase `anbcpdcririfpnxsvwfb`, prefix `deck_backup_` to sit alongside
`deck_github_events`. Follow the supabase-mcp-standard conventions: read/write
tool separation, authed-user connections (never service_role) so RLS applies,
per-user RLS with `stamp_user_id()` / `touch_updated_at()` triggers.

- **`deck_backup_runs`** — one row per agent run: hostname, started_at,
  finished_at, repos_total, repos_ok, repos_failed, bytes_written, exit_status.
- **`deck_backup_artifacts`** — one row per bundle: hostname, repo, kind
  (`full`|`incr`), path, size_bytes, head_sha, basis_manifest JSONB, created_at,
  last_verified_at, verify_method (`bundle_verify`|`restore_test`), verify_ok,
  pruned_at.

Do **not** create a third table for per-repo git state — join to
fleet-git-dashboard's table instead (§3.1) once its name is pinned down.

### 5.6 Widgets — and the actual weave with GitHub watching

Two pages under `cortex-board-widget-style2/src/pages/`:

**`BackupHealthPage`** — per host: last run time, ok/failed counts, stalest repo,
total footprint, and chain status per repo (last `restore_test` and its age).
Anything whose newest verified restore is older than its retention window is red.

**`WorkAtRiskPage`** — the one worth building first, and where your GitHub work
and this system genuinely meet.

fleet-git-dashboard already computes **ahead/behind against the remote**.
`deck_github_events` already knows what is on GitHub. Cross them and you can
answer the question that actually matters:

> *Which repos hold commits that exist on exactly one physical machine and
> nowhere else?*

Those are the only repos where a backup is load-bearing — everything already
pushed to GitHub is replicated by definition. Rank by `ahead > 0`, dirty working
tree, or no remote at all, then overlay backup freshness. A repo that is 12
commits ahead with no remote and no verified restore in 9 days is the top line of
that widget.

This also reframes the nightly cost. Today all 56 repos get equal treatment and
5.27 GB re-uploads nightly, when most of them are fully mirrored on GitHub. Risk
ranking lets backup effort follow actual exposure.

### 5.7 Secrets — treat as security work, not cleanup

Confirmed by inspection 2026-09-13 (key *names* read, values never printed):

- `apps/cortex17/cortex17-20260910-082038/.env` and two `cortex17.env.pre-*`
  files — ~11 credential-shaped vars each, including `CORTEX_BRAIN_SYNC_PASSWORD`,
  `CORTEX_STORE_SUPABASE_KEY`, `CORTEX_BRIDGE_N8N_API_KEY`.
- `apps/metacrate40-server/metacrate40-server-config-*.zip` — 24 daily archives
  since 2026-08-16, **each containing a `.env`**. This substantiates the existing
  board task *"SEC: nightly config backup ships service_role key to Dropbox in
  plaintext"*. The producing job runs on the metacrate40-server VM, not on
  PIX3L-SHIFT3R, which is why it is not in this repo.

Note also that **a git bundle contains full history**, so any secret ever
committed to any of the 56 repos is in `fleet/` right now, even if later removed
from the working tree.

**RISK ACCEPTED by Keenan 2026-09-13.** Access to this Dropbox tree is limited to
approved people and systems, so he rates the exposure minimal and has explicitly
deprioritised it. Do not re-raise it as a blocker and do not spend build time on
scanning or encryption. Two things remain worth knowing rather than acting on:

1. The acceptance rests on the access control, not on the contents. If this tree
   is ever shared more widely, put into a public link, or synced to a machine
   outside the approved set, the acceptance no longer holds and this section
   becomes live again.
2. Anything here is also in Dropbox's cloud storage, which is a third party
   regardless of who has share access.

The pre-existing board task *"SEC: nightly config backup ships service_role key
to Dropbox in plaintext"* is Keenan's own and stays open on his call, not this
spec's.

---

## 6. Phases

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | Pin fleet-git-dashboard's write target table | Table name confirmed against the live DB; existing board blocker closed |
| **1** | `deck_backup_*` schema + agent reports runs/artifacts | Tonight's real run visible on the board with honest counts |
| ~~**1b**~~ | ~~Secret scan + remediation decision~~ | **Dropped 2026-09-13 — risk accepted by Keenan (§5.7). Not in scope.** |
| **2** | Discovery replaces static list | Agent on PIX3L-SHIFT3R finds ≥56 repos with no path array |
| **3** | Incremental engine + manifests | Nightly delta < 50 MB across the fleet; full weekly |
| **4** | Restore-test validator + ledger | Every chain has a real restore test; retention respects the known-good floor |
| **5** | `WorkAtRiskPage`, then `BackupHealthPage` | Both render live data signed in |
| **6** | Roll agent to laptop (`S:\dev`) + VMs | ≥3 hosts writing to `fleet/<HOST>/` with no collisions |

Phases 3 and 4 ship together or not at all. Incrementals without restore testing
are strictly worse than the nightly fulls running today, because they replace a
set of independently-restorable files with a chain nobody has proven.

---

## 7. Open questions for Keenan

1. **Retention window** once incrementals land — 7 fulls is ~7 weeks of history at
   weekly cadence, versus 7 days today. Keep 7, or set a time window?
2. **Restore-test cadence.** A full fleet restore test is expensive; `supabase-fork`
   alone is 2.08 GB. Rotate a few repos nightly, or all weekly?
3. ~~**Secret remedy** (§5.7)~~ — **answered 2026-09-13: risk accepted, do nothing.**
4. **Does Dropbox remain the destination** once risk ranking exists? Most of the
   34.4 GB duplicates GitHub.

## 8. Non-goals for v1

Restoring anything automatically; backing up non-git data beyond what already
sits in `apps/`; deduplication or content-addressed storage; backing up the
phone/tablet (interface nodes, not compute — see fleet-topology).

---

## 9. Reference

| Thing | Path / id |
|---|---|
| Backup tool | `D:\DEV\TOOLS\repo-backup\` (PIX3L-SHIFT3R) |
| Backup root | `S:\Aperture Eclipse Dropbox\Backups\` |
| Repo discovery | `D:\DEV\PROJECTS\TOOLS_Custom_MCPs\fleet-git-dashboard\` |
| Task runner | `D:\DEV\PROJECTS\TOOLS_Custom_MCPs\cortex-task-runner\` |
| Admin deck | `D:\DEV\PROJECTS\cortex-board-widget-style2\` |
| deck-board MCP | `D:\DEV\PROJECTS\TOOLS_Custom_MCPs\deck-board-mcp\` |
| Supabase | `anbcpdcririfpnxsvwfb` (cortex17-docjock-v2), `deck_` prefix |
| Scheduled task | `RepoBackup-Daily`, 03:00 local, user `djunohoo` |
