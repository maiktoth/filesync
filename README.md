# filesync

One-way file sync script for macOS. Designed for external hard drives.  
Runs in **simulation mode by default** — no files are touched unless you pass `--execute`.

## Features

- **Safe by default** — dry-run mode shows exactly what would happen
- **Smart comparison** — skip files that are identical by name + size (or MD5 checksum with `--checksum`)
- **Conflict resolution** — when the same filename exists on both sides but differs, the older copy is renamed `_old_filename` automatically; if the older copy is on the **source**, you are prompted first
- **macOS excludes** — automatically ignores `.DS_Store`, `.Spotlight-V100`, `.Trashes`, etc.
- **Resume support** — interrupted syncs can continue where they left off (`--resume`)
- **Structured logs** — timestamped transfer log, error log, and JSON summary per run
- **macOS notifications** — native notification on completion or failure

## Usage

```bash
# Dry-run (simulation) — safe to run any time
python filesync.py /Volumes/DriveA /Volumes/DriveB

# Apply changes
python filesync.py /Volumes/DriveA /Volumes/DriveB --execute

# Use MD5 checksum for airtight comparison (slower)
python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --checksum

# Also delete target files that no longer exist in source
python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --delete

# Resume an interrupted sync
python filesync.py /Volumes/DriveA /Volumes/DriveB --execute --resume

# Show skipped/identical files in console output
python filesync.py /Volumes/DriveA /Volumes/DriveB --verbose
```

## Options

| Flag | Description |
|------|-------------|
| `--execute` | Apply changes (default is simulation) |
| `--checksum` | Compare files by MD5 instead of size only |
| `--delete` | Remove target files absent from source |
| `--resume` | Skip files already completed in a previous run |
| `--verbose` | Print skipped/identical files to console |
| `--version` | Show version |

## Conflict Resolution

| Scenario | Action |
|----------|--------|
| File only in source | Copied to target |
| File identical (name + size / checksum) | Skipped |
| File differs, **source is newer** | Target renamed to `_old_filename`, source copied |
| File differs, **target is newer** | You are prompted: rename source to `_old_filename`? Options: `y` / `n` / `A`ll / `S`kip all |

## Logs

All logs are written to the `logs/` folder next to the script:

| File | Contents |
|------|----------|
| `transfer_<timestamp>_<MODE>.log` | Full operation log |
| `error_<timestamp>_<MODE>.log` | Errors and warnings only |
| `summary_<timestamp>_<MODE>.json` | Machine-readable run summary |
| `resume_<hash>.json` | Resume state (created with `--resume`) |

## Requirements

- Python 3.10+ (uses walrus operator `:=`)
- macOS (for notifications via `osascript`)
- No external dependencies

## Example Output

```
──────────────────────────────────────────────────────────────
  filesync v1.0.0  |  SIMULATION  |  2026-05-04_14-30-00
──────────────────────────────────────────────────────────────
  source   : /Volumes/DriveA
  target   : /Volumes/DriveB
  checksum : no (size only)
  delete   : no
  resume   : no
──────────────────────────────────────────────────────────────
  *** SIMULATION MODE — no files will be modified ***
  *** Pass --execute to apply changes              ***
──────────────────────────────────────────────────────────────
[SIM] COPY         Documents/report.pdf  (4.2 MB)
[SIM] SKIP         Photos/IMG_001.jpg  (identical)
[SIM] CONFLICT     Music/track.mp3  (source newer)
[SIM]   RENAME target → _old_track.mp3
[SIM]   COPY   source  → target
──────────────────────────────────────────────────────────────
  SUMMARY  (SIMULATION)
──────────────────────────────────────────────────────────────
  Copied               :      1  (4.2 MB)
  Skipped (identical)  :      1
  Conflicts (tgt old)  :      1  (target renamed _old_*)
  Errors               :      0
  Elapsed              :  0.3s
──────────────────────────────────────────────────────────────
```
