#!/usr/bin/env python3
"""
filecompare.py — Read-only comparison of Source vs. Target directory.

Reports differences without touching any files.

Usage:
  python3 filecompare.py <source> <target> [options]

Options:
  --checksum    Use MD5 checksum for content comparison (slower, more accurate)
  --verbose     Also list identical files
"""

import argparse
import hashlib
import json
import logging
import os
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def is_excluded(path: Path) -> bool:
    if any(part.startswith(".") for part in path.parts):
        return True
    return any(part in MACOS_EXCLUDES for part in path.parts)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def md5(path: Path, chunk: int = 65536) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()


def files_differ(src: Path, dst: Path, use_checksum: bool) -> bool:
    if src.stat().st_size != dst.stat().st_size:
        return True
    if use_checksum:
        return md5(src) != md5(dst)
    src_mtime = src.stat().st_mtime
    dst_mtime = dst.stat().st_mtime
    return abs(src_mtime - dst_mtime) > 1  # 1-second tolerance


# ── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(run_id: str, verbose: bool) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"compare_{run_id}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    log = logging.getLogger("compare")
    log.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(console)

    return log, log_path


# ── Compare engine ─────────────────────────────────────────────────────────────

def compare(
    source: Path,
    target: Path,
    *,
    use_checksum: bool,
    log: logging.Logger,
    verbose: bool,
) -> dict:
    stats = {
        "only_in_source": 0,
        "only_in_target": 0,
        "differ": 0,
        "identical": 0,
        "errors": 0,
    }

    # Collect all relative paths from both sides
    def collect(root: Path) -> dict[str, Path]:
        result = {}
        for dirpath, dirs, files in os.walk(root):
            dirpath = Path(dirpath)
            dirs[:] = sorted(d for d in dirs if not is_excluded(dirpath / d))
            rel_root = dirpath.relative_to(root)
            for fname in sorted(files):
                f = dirpath / fname
                if not is_excluded(f):
                    result[str(rel_root / fname)] = f
        return result

    src_files = collect(source)
    dst_files = collect(target)

    all_keys = sorted(set(src_files) | set(dst_files))

    for rel in all_keys:
        in_src = rel in src_files
        in_dst = rel in dst_files

        if in_src and not in_dst:
            src_size = human_size(src_files[rel].stat().st_size)
            log.info(f"ONLY_IN_SOURCE  {rel}  ({src_size})")
            stats["only_in_source"] += 1

        elif in_dst and not in_src:
            dst_size = human_size(dst_files[rel].stat().st_size)
            log.info(f"ONLY_IN_TARGET  {rel}  ({dst_size})")
            stats["only_in_target"] += 1

        else:
            src_file = src_files[rel]
            dst_file = dst_files[rel]
            try:
                differ = files_differ(src_file, dst_file, use_checksum)
            except Exception as exc:
                log.error(f"COMPARE_ERROR   {rel}: {exc}")
                stats["errors"] += 1
                continue

            if differ:
                src_mtime = datetime.fromtimestamp(src_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                dst_mtime = datetime.fromtimestamp(dst_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                src_size = human_size(src_file.stat().st_size)
                dst_size = human_size(dst_file.stat().st_size)
                log.info(
                    f"DIFFER          {rel}\n"
                    f"                  src: {src_size:>10}  modified {src_mtime}\n"
                    f"                  dst: {dst_size:>10}  modified {dst_mtime}"
                )
                stats["differ"] += 1
            else:
                if verbose:
                    log.debug(f"IDENTICAL       {rel}")
                stats["identical"] += 1

    return stats


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="filecompare",
        description="Read-only comparison of Source vs. Target directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python3 filecompare.py /Volumes/DriveA /Volumes/DriveB
  python3 filecompare.py /Volumes/DriveA /Volumes/DriveB --checksum
  python3 filecompare.py /Volumes/DriveA /Volumes/DriveB --verbose
""",
    )
    parser.add_argument("source", type=Path, help="Source directory")
    parser.add_argument("target", type=Path, help="Target directory")
    parser.add_argument("--checksum", action="store_true", help="Use MD5 for content comparison (slower, more accurate)")
    parser.add_argument("--verbose", action="store_true", help="Also list identical files")
    parser.add_argument("--version", action="version", version=f"filecompare {VERSION}")

    args = parser.parse_args()

    for label, path in [("Source", args.source), ("Target", args.target)]:
        if not path.exists():
            print(f"ERROR: {label} path does not exist: {path}", file=sys.stderr)
            sys.exit(1)
        if not path.is_dir():
            print(f"ERROR: {label} path is not a directory: {path}", file=sys.stderr)
            sys.exit(1)

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log, log_path = setup_logging(run_id, args.verbose)

    separator = "─" * 62
    log.info(separator)
    log.info(f"  filecompare v{VERSION}  |  {run_id}")
    log.info(separator)
    log.info(f"  source   : {args.source}")
    log.info(f"  target   : {args.target}")
    log.info(f"  checksum : {'yes (MD5)' if args.checksum else 'no (size + mtime)'}")
    log.info(separator)

    start = datetime.now()
    try:
        stats = compare(
            args.source,
            args.target,
            use_checksum=args.checksum,
            log=log,
            verbose=args.verbose,
        )
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(130)

    elapsed = (datetime.now() - start).total_seconds()

    log.info(separator)
    log.info(f"  SUMMARY")
    log.info(separator)
    log.info(f"  Only in source  : {stats['only_in_source']:>6}")
    log.info(f"  Only in target  : {stats['only_in_target']:>6}")
    log.info(f"  Differ          : {stats['differ']:>6}")
    log.info(f"  Identical       : {stats['identical']:>6}")
    log.info(f"  Errors          : {stats['errors']:>6}")
    log.info(f"  Elapsed         : {elapsed:.1f}s")
    log.info(separator)
    log.info(f"  Log → {log_path.name}")
    log.info(separator)

    summary = {
        "run_id": run_id,
        "source": str(args.source),
        "target": str(args.target),
        "options": {"checksum": args.checksum},
        "elapsed_seconds": round(elapsed, 2),
        "stats": stats,
    }
    summary_path = LOG_DIR / f"compare_summary_{run_id}.json"
    summary_path.write_text(json.dumps(summary, indent=2))

    if stats["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
