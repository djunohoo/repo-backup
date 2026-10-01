# Repo-backup — automatic git-bundle backups to Dropbox

Every configured repo gets bundled to Dropbox once per scheduled run.
Bundle = a self-contained git archive of ALL refs (every branch, every tag,
every stash, every remote). One `.bundle` file per repo per run.

Recovery from any bundle, on any machine, without needing GitHub:

```
git clone <name>.bundle recovered-repo
cd recovered-repo
git checkout <ref-you-want>
```

## Files here

- `backup_repos.py` — the script
- `config.json` — what to back up, where, retention count
- `backup.log` — append-only log, one line per event (created on first run)

## Destination layout

```
S:\Aperture Eclipse Dropbox\Organized\DEV\Backups\repos\
  <repo-name>\
    <repo-name>_<UTC-timestamp>_<short-HEAD>.bundle
    (up to retention_count files, oldest deleted after a new one verifies)
```

## Scheduled task

Registered with Windows Task Scheduler as **RepoBackup-Daily**, runs
daily at 03:00 local time. Check the task's history in Task Scheduler
for pass/fail. Non-zero exit from the script marks the run failed —
open `backup.log` for the details.

Manual run any time:

```
python D:\DEV\TOOLS\repo-backup\backup_repos.py
```

Exit code 0 = every configured repo produced a verified bundle.
Exit code 1 = at least one failure (details in `backup.log`).
Exit code 2 = config load failed.

## Adding or removing repos

Edit `config.json`, add/remove entries in the `repos` array. No restart
needed — the script re-reads the config every run. Paths are absolute.

## Changing retention

Edit `config.json` `retention_count`. The next run applies the new value
by trimming after it writes the new bundle.

## Verification

Every bundle is verified with `git bundle verify` immediately after
creation. If verify fails, the bundle is **deleted** (so a corrupt file
never lingers pretending to be a recoverable backup) and the run reports
failure for that repo. The old bundles for that repo stay — rotation
only trims AFTER the new one verifies.

## Storage math

At ~200MB average per repo * 55 repos * 7 retained = ~75GB steady-state
in Dropbox. Lower `retention_count` if space is a concern. Truly large
repos (Soul-Aperature ~1GB) dominate the total.

## Silent-failure-guard

The script is written to fail loudly:

- Every bundle write logs its actual byte count.
- A bundle that fails `git bundle verify` is deleted; the run does not
  claim success for that repo.
- Rotation happens only after a new bundle verifies — a bad new bundle
  never causes a good old one to be pruned.
- Final line of every run reads `DONE ok=N/M failed=K bytes=...` with
  real counts. If you see `failed=0` in a run, that means zero failed,
  not zero-checked.

## Recovering the whole fleet from Dropbox

If D:\ dies, install Dropbox on a new machine, wait for sync, then:

```
cd "S:\Aperture Eclipse Dropbox\Organized\DEV\Backups\repos\<repo>"
git clone <newest-bundle-file>.bundle <repo-name>
```

Every configured repo can be restored this way independently.
