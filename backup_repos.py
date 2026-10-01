#!/usr/bin/env python3
"""
Repo-backup: bundle every configured git repo to Dropbox on a schedule.

For each repo in config.json:
  1. `git bundle create` a self-contained archive of ALL refs (branches,
     tags, stash, remotes) into the configured destination.
  2. `git bundle verify` the bundle immediately. A bundle that can't verify
     is DELETED — a lie-of-a-backup is worse than a missing one.
  3. Rotate: keep the newest N bundles per repo, delete the rest.

Silent-failure-guard applied throughout:
  - Every successful bundle logs its actual byte count.
  - Every failure logs the repo, the phase, and the underlying error.
  - Final summary reports real counts (created / skipped / failed / rotated).
  - Exit code is 0 only if every configured repo produced a verified bundle.
    Any failure -> non-zero exit -> Windows Task Scheduler surfaces it.

Config file: repo-backup/config.json — see README.md for schema.
Log file:    repo-backup/backup.log — append-only, one line per event.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "backup.log"

MOVE_ATTEMPTS = 5
MOVE_BACKOFF_SECONDS = 2

# Derived, never configured. Several machines back up into one shared Dropbox
# root, and many repo names exist on more than one of them — a hand-written
# hostname copied along with config.json would silently overwrite another
# machine's bundles.
HOSTNAME = socket.gethostname().upper()


@dataclass
class Config:
    destination: Path
    retention_count: int
    repos: list[Path]
    skip_missing: bool = True
    staging_dir: Path = HERE / ".staging"


@dataclass
class RepoResult:
    repo: Path
    ok: bool
    bundle_path: Optional[Path] = None
    bytes_written: int = 0
    head_short: str = ""
    error: str = ""
    rotated_count: int = 0


@dataclass
class RunSummary:
    started_at: str = ""
    finished_at: str = ""
    results: list[RepoResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)


def log(msg: str) -> None:
    """Append one timestamped line to the log AND stdout."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{stamp} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"config.json missing at {CONFIG_PATH}")
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dest = Path(raw["destination"])
    retention = int(raw.get("retention_count", 7))
    repos = [Path(p) for p in raw.get("repos", [])]
    skip_missing = bool(raw.get("skip_missing", True))
    staging = Path(raw["staging_dir"]) if raw.get("staging_dir") else HERE / ".staging"
    if not repos:
        raise SystemExit("config.json has no repos configured")
    return Config(
        destination=dest,
        retention_count=retention,
        repos=repos,
        skip_missing=skip_missing,
        staging_dir=staging,
    )


def run_git(repo: Path, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run `git -C <repo> <args>` with captured output and a hard timeout.

    Timeout matters: a hung git process on a broken repo would otherwise wedge
    the whole nightly run. 5 minutes is generous even for a 1GB repo bundle.
    """
    return subprocess.run(
        ["git", "-C", str(repo)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def short_head(repo: Path) -> str:
    """Best-effort short HEAD; empty string if the repo can't be read.

    Bundled into the filename so a human can tell backups apart at a glance.
    """
    try:
        r = run_git(repo, ["rev-parse", "--short", "HEAD"], timeout=15)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return "unknown"


def bundle_one(repo: Path, cfg: Config) -> RepoResult:
    """Bundle + verify + rotate one repo. Never raises — returns a RepoResult."""
    result = RepoResult(repo=repo, ok=False)

    if not repo.exists():
        if cfg.skip_missing:
            result.error = "repo path does not exist (skip_missing=true)"
            log(f"SKIP  {repo} — {result.error}")
            return result
        result.error = "repo path does not exist"
        log(f"FAIL  {repo} — {result.error}")
        return result

    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        result.error = "not a git repo (.git and HEAD both missing)"
        log(f"SKIP  {repo} — {result.error}")
        return result

    head = short_head(repo)
    result.head_short = head

    repo_name = repo.name
    # <fleet-root>/<HOST>/repos/<repo>/full — 'full' is a sibling of the 'incr'
    # tree the incremental scheduler will write, so rotation here only ever
    # counts full bundles.
    dest_dir = cfg.destination / HOSTNAME / "repos" / repo_name / "full"
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    bundle_name = f"{repo_name}_{timestamp}_{head}.bundle"
    stage_path = cfg.staging_dir / bundle_name
    bundle_path = dest_dir / bundle_name
    size = 0

    # Bundle and verify on LOCAL disk, then move the finished file into the
    # destination. Writing straight into a Dropbox folder loses a race against
    # Dropbox's filter driver, which grabs newly created files — including the
    # <name>.bundle.lock git writes before renaming — and fails the open with
    # EACCES. That silently cost 6-8 repos per night from 2026-08-26 onward.
    try:
        try:
            create = run_git(repo, ["bundle", "create", str(stage_path), "--all"], timeout=1800)
        except subprocess.TimeoutExpired:
            result.error = "git bundle create timed out (>30min)"
            log(f"FAIL  {repo_name} — {result.error}")
            return result

        if create.returncode != 0:
            result.error = f"git bundle create failed: {create.stderr.strip()[:200]}"
            log(f"FAIL  {repo_name} — {result.error}")
            return result

        # A bundle that won't verify is worse than no bundle — it would lie
        # about being recoverable. Never let an unverified one reach the
        # destination in the first place.
        try:
            verify = run_git(repo, ["bundle", "verify", str(stage_path)], timeout=300)
        except subprocess.TimeoutExpired:
            result.error = "git bundle verify timed out (discarding unverified bundle)"
            log(f"FAIL  {repo_name} — {result.error}")
            return result

        if verify.returncode != 0:
            result.error = f"bundle verify failed: {verify.stderr.strip()[:200]}"
            log(f"FAIL  {repo_name} — {result.error} (discarding unverified bundle)")
            return result

        try:
            size = stage_path.stat().st_size
        except OSError as e:
            result.error = f"staged bundle unreadable: {e}"
            log(f"FAIL  {repo_name} — {result.error}")
            return result

        for attempt in range(1, MOVE_ATTEMPTS + 1):
            try:
                shutil.move(str(stage_path), str(bundle_path))
                break
            except OSError as e:
                if attempt == MOVE_ATTEMPTS:
                    result.error = f"move into destination failed after {attempt} attempts: {e}"
                    log(f"FAIL  {repo_name} — {result.error}")
                    return result
                time.sleep(MOVE_BACKOFF_SECONDS * attempt)
    finally:
        # Staging must never accumulate; an aborted run would otherwise strand
        # gigabytes of orphaned bundles on the local disk.
        if stage_path.exists():
            try:
                stage_path.unlink()
            except OSError:
                pass

    result.bytes_written = size
    result.bundle_path = bundle_path

    # 3) Rotate: keep the newest N (by mtime), delete the rest.
    #    Do this ONLY after the new bundle is verified — otherwise a failed
    #    verify + successful rotate would trim a good older bundle in favor
    #    of a bad new one.
    existing = sorted(
        (p for p in dest_dir.iterdir() if p.is_file() and p.suffix == ".bundle"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = existing[cfg.retention_count:]
    for p in to_delete:
        try:
            p.unlink()
            result.rotated_count += 1
        except OSError as e:
            log(f"WARN  {repo_name} — could not rotate {p.name}: {e}")

    result.ok = True
    log(
        f"OK    {repo_name} @ {head} -> {bundle_name} "
        f"({size:,} bytes, rotated {result.rotated_count})"
    )
    return result


def main() -> int:
    summary = RunSummary()
    summary.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        cfg = load_config()
    except SystemExit as e:
        log(f"ABORT config load failed: {e}")
        return 2

    cfg.destination.mkdir(parents=True, exist_ok=True)
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)

    log(
        f"START  {len(cfg.repos)} repos -> {cfg.destination} "
        f"(retention={cfg.retention_count}, staging={cfg.staging_dir})"
    )

    for repo in cfg.repos:
        summary.results.append(bundle_one(repo, cfg))

    summary.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_bytes = sum(r.bytes_written for r in summary.results if r.ok)

    log(
        f"DONE   ok={summary.ok_count}/{len(summary.results)} "
        f"failed={summary.failed_count} bytes={total_bytes:,}"
    )

    # Non-zero exit if any repo failed — Task Scheduler will surface it as a
    # failed run (visible in the task history) rather than silently succeeding.
    return 0 if summary.failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
