#!/usr/bin/env bash
# sync_to_onedrive.sh
# Syncs an external HDD to OneDrive folder-by-folder using rclone.
# Edit the CONFIG section before first use.

set -euo pipefail

# ─── CONFIG ──────────────────────────────────────────────────────────────────
RCLONE_REMOTE="onedrive"                          # rclone remote name
SOURCE_PATH="/Volumes/MyExternalDrive"            # source: external HDD mount point
DEST_PATH="onedrive:Backup/ExternalDrive"         # destination: remote:path
LOG_FILE="$HOME/Library/Logs/onedrive_sync.log"  # log file location
RCLONE_EXTRA_FLAGS="--transfers 4 --checkers 8 --fast-list"
# ─────────────────────────────────────────────────────────────────────────────

RUN_TIMESTAMP=$(date '+%Y-%m-%d_%H-%M-%S')
SEPARATOR="────────────────────────────────────────────────────────────"

# Counters
total_folders=0
success_folders=0
error_folders=0
total_files=0
total_bytes=0
declare -a error_list=()

# ── Helpers ───────────────────────────────────────────────────────────────────

log() {
    local msg="$1"
    echo "$msg" | tee -a "$LOG_FILE"
}

bytes_to_human() {
    local bytes=$1
    if   (( bytes >= 1073741824 )); then printf "%.2f GB" "$(echo "scale=2; $bytes/1073741824" | bc)"
    elif (( bytes >= 1048576   )); then printf "%.2f MB" "$(echo "scale=2; $bytes/1048576"    | bc)"
    elif (( bytes >= 1024      )); then printf "%.2f KB" "$(echo "scale=2; $bytes/1024"       | bc)"
    else printf "%d B" "$bytes"
    fi
}

# ── Pre-flight checks ─────────────────────────────────────────────────────────

if ! command -v rclone &>/dev/null; then
    echo "ERROR: rclone not found. Install it with: brew install rclone" >&2
    exit 1
fi

if [[ ! -d "$SOURCE_PATH" ]]; then
    echo "ERROR: Source path not found: $SOURCE_PATH" >&2
    echo "Make sure your external drive is mounted." >&2
    exit 1
fi

if ! rclone lsd "${RCLONE_REMOTE}:" &>/dev/null; then
    echo "ERROR: rclone remote '${RCLONE_REMOTE}' is not accessible." >&2
    echo "Run 'rclone config' to set it up." >&2
    exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"

# ── Run header ────────────────────────────────────────────────────────────────

log ""
log "$SEPARATOR"
log "  OneDrive Sync — Run started: $RUN_TIMESTAMP"
log "  Source : $SOURCE_PATH"
log "  Dest   : $DEST_PATH"
log "  Log    : $LOG_FILE"
log "$SEPARATOR"

# ── Collect top-level folders ─────────────────────────────────────────────────

mapfile -d '' folders < <(find "$SOURCE_PATH" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

if [[ ${#folders[@]} -eq 0 ]]; then
    log "WARNING: No subdirectories found in $SOURCE_PATH. Nothing to sync."
    exit 0
fi

log "  Folders to sync: ${#folders[@]}"
log "$SEPARATOR"

# ── Per-folder sync ───────────────────────────────────────────────────────────

for folder in "${folders[@]}"; do
    folder_name=$(basename "$folder")
    dest_folder="${DEST_PATH}/${folder_name}"
    total_folders=$(( total_folders + 1 ))

    log ""
    log "  [$(date '+%H:%M:%S')] Syncing: $folder_name"

    # Temp file to capture rclone output
    tmp_out=$(mktemp)

    # Run rclone sync; capture stderr+stdout; don't let failure abort script
    sync_status="success"
    if ! rclone sync \
            "$folder" \
            "$dest_folder" \
            $RCLONE_EXTRA_FLAGS \
            --stats 1000h \
            --stats-one-line \
            --log-level INFO \
            --log-file "$tmp_out" \
            2>&1; then
        sync_status="error"
    fi

    # Parse stats from rclone log output
    # rclone --stats-one-line format:
    #   Transferred: X files, Y Bytes, Z/s, ETA ...
    transferred_files=0
    transferred_bytes=0

    if grep -qE 'Transferred:' "$tmp_out" 2>/dev/null; then
        # Extract file count (last "Transferred:" line)
        files_line=$(grep 'Transferred:' "$tmp_out" | tail -1)

        # Files transferred (integer before " files")
        if [[ "$files_line" =~ ([0-9]+)[[:space:]]*/[[:space:]]*[0-9]+[[:space:]]file ]]; then
            transferred_files="${BASH_REMATCH[1]}"
        elif [[ "$files_line" =~ Transferred:[[:space:]]*([0-9]+)[[:space:]]file ]]; then
            transferred_files="${BASH_REMATCH[1]}"
        fi

        # Bytes transferred — rclone outputs e.g. "1.234 GiByte" or "512 kiByte"
        bytes_line=$(grep 'Transferred:' "$tmp_out" | grep -v ' files' | tail -1 || true)
        if [[ -z "$bytes_line" ]]; then
            bytes_line="$files_line"
        fi

        # Try to extract a human-readable size token like "1.23 GiByte", "456 MiByte"
        if [[ "$bytes_line" =~ ([0-9]+\.?[0-9]*)[[:space:]]*(GiByte|MiByte|kiByte|Byte) ]]; then
            raw_num="${BASH_REMATCH[1]}"
            raw_unit="${BASH_REMATCH[2]}"
            case "$raw_unit" in
                GiByte) transferred_bytes=$(echo "scale=0; $raw_num * 1073741824 / 1" | bc) ;;
                MiByte) transferred_bytes=$(echo "scale=0; $raw_num * 1048576 / 1"    | bc) ;;
                kiByte) transferred_bytes=$(echo "scale=0; $raw_num * 1024 / 1"       | bc) ;;
                Byte)   transferred_bytes=$(printf "%.0f" "$raw_num") ;;
            esac
        fi
    fi

    # Append rclone log to the main log
    cat "$tmp_out" >> "$LOG_FILE"
    rm -f "$tmp_out"

    total_files=$(( total_files + transferred_files ))
    total_bytes=$(( total_bytes + transferred_bytes ))

    human_size=$(bytes_to_human "$transferred_bytes")

    if [[ "$sync_status" == "success" ]]; then
        success_folders=$(( success_folders + 1 ))
        status_icon="✓"
    else
        error_folders=$(( error_folders + 1 ))
        error_list+=("$folder_name")
        status_icon="✗"
    fi

    # ── Checkpoint summary ────────────────────────────────────────────────────
    log ""
    log "  ┌─ Checkpoint: $folder_name"
    log "  │  Status    : $status_icon $sync_status"
    log "  │  Files     : $transferred_files transferred"
    log "  │  Data      : $human_size"
    log "  │  Timestamp : $(date '+%Y-%m-%d %H:%M:%S')"
    log "  └──────────────────────────────────────────────"
done

# ── Final summary ─────────────────────────────────────────────────────────────

total_human=$(bytes_to_human "$total_bytes")

log ""
log "$SEPARATOR"
log "  FINAL SUMMARY — $RUN_TIMESTAMP"
log "$SEPARATOR"
log "  Folders processed : $total_folders"
log "  Successful        : $success_folders"
log "  Errors            : $error_folders"
log "  Files transferred : $total_files"
log "  Data synced       : $total_human"

if [[ ${#error_list[@]} -gt 0 ]]; then
    log ""
    log "  Failed folders:"
    for ef in "${error_list[@]}"; do
        log "    • $ef"
    done
fi

log "$SEPARATOR"
log ""

# Exit with non-zero if any folder failed
[[ $error_folders -eq 0 ]] && exit 0 || exit 1
