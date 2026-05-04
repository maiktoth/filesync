#!/usr/bin/env python3
"""
filesync.py — One-way file sync (Source → Target) for macOS.

Default: SIMULATION mode (dry-run). No files are changed unless --execute is passed.

Usage:
  python filesync.py <source> <target> [options]

Options:
  --execute          Apply changes (default is simulation)
  --checksum         Use MD5 checksum for file comparison (slower, more accurate)
  --delete           Remove target files that no longer exist in source
  --resume           Skip files already recorded in the resume state file
  --verbose          Show skipped files in console output
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

VERSION = "1.0.0"

MACOS_EXCLUDES = frozenset({
    ".DS_Store",
    ".Spotlight-V100",
    ".Trashes",
    ".fseventsd",
    ".TemporaryItems",
    ".VolumeIcon.icns",
    "System Volume Information",
    ".com.apple.timemachine.donotpresent",
    ".DocumentRevisions-V100",
})

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"

# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(run_id: str, simulate: bool, verbose: bool) -> tuple[logging.Logger, logging.Logger]:
    LOG_DIR.mkdir(exist_ok=True)
    mode_tag = "SIM" if simulate else "EXEC"

    transfer_path = LOG_DIR / f"transfer_{run_id}_{mode_tag}.log"
    error_path = LOG_DIR / f"error_{run_id}_{mode_tag}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    tlog = logging.getLogger("transfer")
    tlog.setLevel(logging.DEBUG)
    th = logging.FileHandler(transfer_path, encoding="utf-8")
    th.setFormatter(fmt)
    tlog.addHandler(th)

    elog = logging.getLogger("error")
    elog.setLevel(logging.WARNING)
    eh = logging.FileHandler(error_path, encoding="utf-8")
    eh.setFormatter(fmt)
    elog.addHandler(eh)
    elog.addHandler(logging.StreamHandler(sys.stderr))

    # Console: INFO by default, DEBUG if verbose
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    tlog.addHandler(console)

    return tlog, elog


# ── macOS notification ─────────────────────────────────────────────────────────

def notify(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=5)
    except Exception:
        pass  # best-effort


# ── File comparison ────────────────────────────────────────────────────────────

def md5(path: Path, chunk: int = 65536) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()


def files_identical(src: Path, dst: Path, use_checksum: bool) -> bool:
    if src.stat().st_size != dst.stat().st_size:
        return False
    return md5(src) == md5(dst) if use_checksum else True


# ── Path helpers ───────────────────────────────────────────────────────────────

def is_excluded(path: Path) -> bool:
    return any(part in MACOS_EXCLUDES for part in path.parts)


def prefixed_old(p: Path) -> Path:
    return p.parent / f"_old_{p.name}"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── Resume state ───────────────────────────────────────────────────────────────

class ResumeState:
    """Tracks successfully copied files between interrupted runs."""

    def __init__(self, source: Path, target: Path, active: bool):
        self.active = active
        self._path = LOG_DIR / f"resume_{_path_hash(source, target)}.json"
        self._done: set[str] = set()
        if active and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._done = set(data.get("done", []))
                print(f"[resume] Loaded {len(self._done)} already-completed entries from {self._path.name}")
            except Exception:
                self._done = set()

    def is_done(self, rel: str) -> bool:
        return self.active and rel in self._done

    def mark_done(self, rel: str) -> None:
        if not self.active:
            return
        self._done.add(rel)
        self._path.write_text(json.dumps({"done": list(self._done)}, indent=2))

    def clear(self) -> None:
        self._done.clear()
        if self._path.exists():
            self._path.unlink()


def _path_hash(source: Path, target: Path) -> str:
    return hashlib.md5(f"{source}{target}".encode()).hexdigest()[:8]


# ── Interactive confirmation ───────────────────────────────────────────────────

class ConfirmState:
    """Handles per-file or blanket confirmation for source-side modifications."""

    def __init__(self):
        self._rename_all = False
        self._skip_all = False

    def ask_rename_source(self, src: Path, dst: Path) -> bool:
        """
        Returns True if the user agrees to rename the older source file to _old_*.
        Caches blanket choices (All / Skip all).
        """
        if self._skip_all:
            return False
        if self._rename_all:
            return True

        src_dt = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        dst_dt = datetime.fromtimestamp(dst.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        print()
        print("  ┌─ CONFLICT: target is NEWER than source ──────────────────────────")
        print(f"  │  source : {src}")
        print(f"  │           modified {src_dt}  ({human_size(src.stat().st_size)})")
        print(f"  │  target : {dst}")
        print(f"  │           modified {dst_dt}  ({human_size(dst.stat().st_size)})")
        print(f"  │")
        print(f"  │  Rename source → _old_{src.name} ?")
        print(f"  │  [y] Yes (this file)  [n] No / skip  [A] Yes to ALL  [S] Skip ALL  ", end="")

        choice = input().strip()
        if choice == "y":
            return True
        if choice == "A":
            self._rename_all = True
            return True
        if choice == "S":
            self._skip_all = True
            return False
        return False


# ── Core sync engine ───────────────────────────────────────────────────────────

def sync(
    source: Path,
    target: Path,
    *,
    use_checksum: bool,
    simulate: bool,
    delete: bool,
    tlog: logging.Logger,
    elog: logging.Logger,
    confirm: ConfirmState,
    resume: ResumeState,
) -> dict:
    stats = {
        "copied": 0,
        "skipped_identical": 0,
        "skipped_resume": 0,
        "conflicts_target_renamed": 0,
        "conflicts_source_renamed": 0,
        "conflicts_skipped": 0,
        "deleted": 0,
        "errors": 0,
        "bytes_copied": 0,
    }

    pfx = "[SIM]" if simulate else "[EXEC]"

    # ── Walk source ────────────────────────────────────────────────────────────
    for src_root, dirs, files in os.walk(source):
        src_root = Path(src_root)
        dirs[:] = sorted(d for d in dirs if not is_excluded(src_root / d))

        rel_root = src_root.relative_to(source)
        dst_root = target / rel_root

        for fname in sorted(files):
            src_file = src_root / fname
            dst_file = dst_root / fname
            rel = str(rel_root / fname)

            if is_excluded(src_file):
                continue

            # Resume: skip already-completed files
            if resume.is_done(rel):
                tlog.debug(f"{pfx} RESUME-SKIP  {rel}")
                stats["skipped_resume"] += 1
                continue

            # ── No target file → copy ──────────────────────────────────────
            if not dst_file.exists():
                size = src_file.stat().st_size
                tlog.info(f"{pfx} COPY         {rel}  ({human_size(size)})")
                if not simulate:
                    try:
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_file, dst_file)
                        stats["bytes_copied"] += size
                        resume.mark_done(rel)
                    except Exception as exc:
                        elog.error(f"COPY FAILED   {rel}: {exc}")
                        stats["errors"] += 1
                        continue
                else:
                    stats["bytes_copied"] += size
                stats["copied"] += 1
                continue

            # ── Target file exists → compare ───────────────────────────────
            try:
                identical = files_identical(src_file, dst_file, use_checksum)
            except Exception as exc:
                elog.error(f"COMPARE FAILED {rel}: {exc}")
                stats["errors"] += 1
                continue

            if identical:
                tlog.debug(f"{pfx} SKIP         {rel}  (identical)")
                stats["skipped_identical"] += 1
                resume.mark_done(rel)
                continue

            # ── Conflict: same name, different content ─────────────────────
            src_mtime = src_file.stat().st_mtime
            dst_mtime = dst_file.stat().st_mtime

            if src_mtime >= dst_mtime:
                # Source is newer → rename OLD target, then copy source
                old_dst = prefixed_old(dst_file)
                tlog.info(f"{pfx} CONFLICT     {rel}  (source newer)")
                tlog.info(f"{pfx}   RENAME target → _old_{fname}")
                tlog.info(f"{pfx}   COPY   source  → target")
                if not simulate:
                    try:
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        if old_dst.exists():
                            old_dst.unlink()
                        dst_file.rename(old_dst)
                        shutil.copy2(src_file, dst_file)
                        stats["bytes_copied"] += src_file.stat().st_size
                        resume.mark_done(rel)
                    except Exception as exc:
                        elog.error(f"CONFLICT RESOLVE FAILED {rel}: {exc}")
                        stats["errors"] += 1
                        continue
                stats["conflicts_target_renamed"] += 1

            else:
                # Target is newer → source is the older one
                tlog.info(f"{pfx} CONFLICT     {rel}  (target newer — source may need renaming)")
                if not simulate:
                    do_rename = confirm.ask_rename_source(src_file, dst_file)
                    if do_rename:
                        old_src = prefixed_old(src_file)
                        try:
                            if old_src.exists():
                                old_src.unlink()
                            src_file.rename(old_src)
                            tlog.info(f"{pfx}   RENAMED source → _old_{fname}")
                            stats["conflicts_source_renamed"] += 1
                        except Exception as exc:
                            elog.error(f"SOURCE RENAME FAILED {rel}: {exc}")
                            stats["errors"] += 1
                    else:
                        tlog.info(f"{pfx}   SKIPPED (user chose not to rename source)")
                        stats["conflicts_skipped"] += 1
                else:
                    # In simulation, report what would happen without prompting
                    tlog.info(f"{pfx}   → would prompt: rename source to _old_{fname}?")
                    stats["conflicts_skipped"] += 1

    # ── Optional delete pass ───────────────────────────────────────────────────
    if delete:
        for dst_root, dirs, files in os.walk(target):
            dst_root = Path(dst_root)
            dirs[:] = [d for d in dirs if not is_excluded(dst_root / d)]
            rel_root = dst_root.relative_to(target)

            for fname in files:
                dst_file = dst_root / fname
                if is_excluded(dst_file):
                    continue
                src_candidate = source / rel_root / fname
                if not src_candidate.exists():
                    rel = str(rel_root / fname)
                    tlog.info(f"{pfx} DELETE       {rel}  (absent from source)")
                    if not simulate:
                        try:
                            dst_file.unlink()
                        except Exception as exc:
                            elog.error(f"DELETE FAILED {rel}: {exc}")
                            stats["errors"] += 1
                            continue
                    stats["deleted"] += 1

    return stats


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="filesync",
        description=(
            "One-way file sync (Source → Target) for macOS.\n"
            "Runs in SIMULATION mode by default — pass --execute to apply changes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python filesync.py /Volumes/DriveA /Volumes/DriveB
  python filesync.py /Volumes/DriveA /Volumes/DriveB --execute
  python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --checksum
  python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --checksum --delete
  python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --resume
""",
    )
    parser.add_argument("source", type=Path, help="Source directory (treated as read-only unless conflict requires rename)")
    parser.add_argument("target", type=Path, help="Target directory")
    parser.add_argument("--execute", action="store_true", help="Apply changes (default: simulation/dry-run)")
    parser.add_argument("--checksum", action="store_true", help="Use MD5 checksum for file identity check (slower, more accurate)")
    parser.add_argument("--delete", action="store_true", help="Delete files in target that no longer exist in source")
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted sync (skip already-completed files)")
    parser.add_argument("--verbose", action="store_true", help="Show skipped/identical files in output")
    parser.add_argument("--version", action="version", version=f"filesync {VERSION}")

    args = parser.parse_args()
    simulate = not args.execute

    # Validate paths
    for label, path in [("Source", args.source), ("Target", args.target)]:
        if not path.exists():
            print(f"ERROR: {label} path does not exist: {path}", file=sys.stderr)
            sys.exit(1)
        if not path.is_dir():
            print(f"ERROR: {label} path is not a directory: {path}", file=sys.stderr)
            sys.exit(1)

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    tlog, elog = setup_logging(run_id, simulate, args.verbose)

    mode_label = "SIMULATION" if simulate else "EXECUTE"
    separator = "─" * 62

    tlog.info(separator)
    tlog.info(f"  filesync v{VERSION}  |  {mode_label}  |  {run_id}")
    tlog.info(separator)
    tlog.info(f"  source   : {args.source}")
    tlog.info(f"  target   : {args.target}")
    tlog.info(f"  checksum : {'yes (MD5)' if args.checksum else 'no (size only)'}")
    tlog.info(f"  delete   : {'yes' if args.delete else 'no'}")
    tlog.info(f"  resume   : {'yes' if args.resume else 'no'}")
    tlog.info(separator)

    if simulate:
        tlog.info("  *** SIMULATION MODE — no files will be modified ***")
        tlog.info("  *** Pass --execute to apply changes              ***")
        tlog.info(separator)

    confirm = ConfirmState()
    resume = ResumeState(args.source, args.target, args.resume)

    start_time = datetime.now()
    try:
        stats = sync(
            args.source,
            args.target,
            use_checksum=args.checksum,
            simulate=simulate,
            delete=args.delete,
            tlog=tlog,
            elog=elog,
            confirm=confirm,
            resume=resume,
        )
    except KeyboardInterrupt:
        elapsed = (datetime.now() - start_time).total_seconds()
        tlog.warning(f"Interrupted by user after {elapsed:.0f}s.")
        notify("filesync interrupted", "Sync was stopped by the user.")
        sys.exit(130)

    elapsed = (datetime.now() - start_time).total_seconds()

    # ── Summary ────────────────────────────────────────────────────────────────
    tlog.info(separator)
    tlog.info(f"  SUMMARY  ({mode_label})")
    tlog.info(separator)
    tlog.info(f"  Copied               : {stats['copied']:>6}  ({human_size(stats['bytes_copied'])})")
    tlog.info(f"  Skipped (identical)  : {stats['skipped_identical']:>6}")
    tlog.info(f"  Skipped (resume)     : {stats['skipped_resume']:>6}")
    tlog.info(f"  Conflicts (tgt old)  : {stats['conflicts_target_renamed']:>6}  (target renamed _old_*)")
    tlog.info(f"  Conflicts (src old)  : {stats['conflicts_source_renamed']:>6}  (source renamed _old_*)")
    tlog.info(f"  Conflicts (skipped)  : {stats['conflicts_skipped']:>6}")
    tlog.info(f"  Deleted              : {stats['deleted']:>6}")
    tlog.info(f"  Errors               : {stats['errors']:>6}")
    tlog.info(f"  Elapsed              : {elapsed:.1f}s")
    tlog.info(separator)

    # ── JSON summary ───────────────────────────────────────────────────────────
    summary = {
        "run_id": run_id,
        "mode": mode_label,
        "source": str(args.source),
        "target": str(args.target),
        "options": {
            "checksum": args.checksum,
            "delete": args.delete,
            "resume": args.resume,
        },
        "elapsed_seconds": round(elapsed, 2),
        "stats": stats,
    }
    summary_path = LOG_DIR / f"summary_{run_id}_{mode_label}.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    tlog.info(f"  JSON summary → {summary_path.name}")
    tlog.info(separator)

    # ── macOS notification ─────────────────────────────────────────────────────
    if stats["errors"] == 0:
        status_msg = f"{stats['copied']} copied · {stats['skipped_identical']} skipped · {elapsed:.0f}s"
        notify(f"filesync {mode_label}", status_msg)
    else:
        notify(f"filesync {mode_label} — {stats['errors']} ERRORS", f"{stats['copied']} copied · check error log")

    # Exit non-zero if there were errors
    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
