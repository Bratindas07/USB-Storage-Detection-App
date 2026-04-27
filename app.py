"""
USB Monitor — Backend (app.py)
Phase 0 + Phase 1 complete:
  - WMI UUID as drive identity
  - psycopg3 PostgreSQL integration
  - Batch insert after full scan
  - MD5 + SHA256 hashing per file
  - Incremental update (changed files updated, removed files hard-deleted)
  - Scan session tracking
  - Offline keyword search from DB
  - Drive online/offline status management
"""

import os
import time
import queue
import hashlib
import threading
import pythoncom
import wmi
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

import psycopg
from psycopg.rows import dict_row

from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ── DB connection ──────────────────────────────────────────────────────────────
DB_DSN = (
    f"host={os.getenv('PG_HOST', 'localhost')} "
    f"port={os.getenv('PG_PORT', '5432')} "
    f"dbname={os.getenv('PG_DBNAME', 'postgres')} "
    f"user={os.getenv('PG_USER', 'postgres')} "
    f"password={os.getenv('PG_PASSWORD', '1234')} "
    f"connect_timeout=5"
)


def get_conn():
    """Return a new psycopg3 connection with dict rows."""
    return psycopg.connect(DB_DSN, row_factory=dict_row)


def test_connection() -> bool:
    """Return True if DB is reachable."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


# ── WMI helpers ────────────────────────────────────────────────────────────────
def _init_wmi():
    pythoncom.CoInitialize()
    return wmi.WMI()


def get_drive_wmi_uuid(drive_letter: str) -> str | None:
    """
    Return the WMI PnP device instance ID for the USB drive that hosts
    the given logical drive letter.  This survives label/letter changes.
    Falls back to volume serial number if PnP ID is unavailable.
    """
    try:
        c = _init_wmi()
        letter = drive_letter.rstrip("\\").rstrip(":")
        for disk in c.Win32_DiskDrive():
            if disk.InterfaceType != "USB":
                continue
            for part in disk.associators("Win32_DiskDriveToDiskPartition"):
                for logical in part.associators("Win32_LogicalDiskToPartition"):
                    if logical.DeviceID.upper().startswith(letter.upper()):
                        # PnP device instance ID is the most stable identifier
                        uuid = getattr(disk, "PNPDeviceID", None)
                        if uuid:
                            return uuid.strip()
        return None
    except Exception:
        return None


def get_volume_serial(drive_letter: str) -> str | None:
    """Return the volume serial number as a hex string."""
    try:
        import ctypes
        serial = ctypes.c_ulong()
        ctypes.windll.kernel32.GetVolumeInformationW(
            drive_letter if drive_letter.endswith("\\") else drive_letter + "\\",
            None, 0, ctypes.byref(serial), None, None, None, 0
        )
        return format(serial.value, "08X")
    except Exception:
        return None


def get_connected_usb_drives() -> set[str]:
    """Return set of drive letters (e.g. {'E:\\'}) for connected USB drives."""
    try:
        c = _init_wmi()
        drives = set()
        for disk in c.Win32_DiskDrive():
            if disk.InterfaceType == "USB":
                for part in disk.associators("Win32_DiskDriveToDiskPartition"):
                    for logical in part.associators("Win32_LogicalDiskToPartition"):
                        drives.add(logical.DeviceID + "\\")
        return drives
    except Exception:
        return set()


def get_drive_info(drive_letter: str) -> dict:
    """Return label, filesystem, total/used/free GB for a drive letter."""
    try:
        c = _init_wmi()
        letter = drive_letter.rstrip("\\")
        for d in c.Win32_LogicalDisk(DeviceID=letter):
            total = int(d.Size or 0)
            free  = int(d.FreeSpace or 0)
            used  = total - free
            return {
                "label":      d.VolumeName or "No Label",
                "filesystem": d.FileSystem or "Unknown",
                "total_gb":   round(total / (1024 ** 3), 2),
                "used_gb":    round(used  / (1024 ** 3), 2),
                "free_gb":    round(free  / (1024 ** 3), 2),
            }
    except Exception:
        pass
    return {"label": "Unknown", "filesystem": "Unknown",
            "total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0}


# ── Hashing ────────────────────────────────────────────────────────────────────
HASH_SIZE_LIMIT = 2 * 1024 ** 3  # Skip hashing files > 2 GB


def compute_hashes(path: Path) -> dict[str, str]:
    """Return {'MD5': ..., 'SHA256': ...} or empty dict on error."""
    try:
        if path.stat().st_size > HASH_SIZE_LIMIT:
            return {}
        md5    = hashlib.md5()
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                md5.update(chunk)
                sha256.update(chunk)
        return {"MD5": md5.hexdigest().upper(), "SHA256": sha256.hexdigest().upper()}
    except Exception:
        return {}


# ── DB — drive upsert ──────────────────────────────────────────────────────────
def upsert_drive(conn, drive_letter: str) -> tuple[int, dict]:
    """
    Upsert the drive record keyed on wmi_uuid.
    Returns (drive_id, info_dict).
    """
    wmi_uuid = get_drive_wmi_uuid(drive_letter)
    serial   = get_volume_serial(drive_letter)
    info     = get_drive_info(drive_letter)

    if not wmi_uuid:
        # Fallback: use serial number as UUID placeholder
        wmi_uuid = f"SERIAL-{serial}" if serial else f"LETTER-{drive_letter}"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO drives
                (wmi_uuid, serial_number, drive_letter, label, filesystem,
                 total_gb, used_gb, free_gb, is_online, last_seen_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW())
            ON CONFLICT (wmi_uuid) DO UPDATE SET
                drive_letter  = EXCLUDED.drive_letter,
                serial_number = COALESCE(EXCLUDED.serial_number, drives.serial_number),
                label         = EXCLUDED.label,
                filesystem    = EXCLUDED.filesystem,
                total_gb      = EXCLUDED.total_gb,
                used_gb       = EXCLUDED.used_gb,
                free_gb       = EXCLUDED.free_gb,
                is_online     = TRUE,
                last_seen_at  = NOW(),
                total_scans   = drives.total_scans + 1
            RETURNING id
        """, (
            wmi_uuid, serial, drive_letter.rstrip("\\"),
            info["label"], info["filesystem"],
            info["total_gb"], info["used_gb"], info["free_gb"]
        ))
        drive_id = cur.fetchone()["id"]
    conn.commit()
    return drive_id, info


def mark_drive_offline(wmi_uuid: str):
    """Mark a drive as offline in the DB."""
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE drives SET is_online = FALSE WHERE wmi_uuid = %s",
                (wmi_uuid,)
            )
            conn.commit()
    except Exception:
        pass


def set_all_drives_offline():
    """Called on startup to reset stale online flags."""
    try:
        with get_conn() as conn:
            conn.execute("UPDATE drives SET is_online = FALSE")
            conn.commit()
    except Exception:
        pass


# ── DB — scan session ──────────────────────────────────────────────────────────
def start_scan_session(conn, drive_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO scan_sessions (drive_id, status)
            VALUES (%s, 'RUNNING') RETURNING id
        """, (drive_id,))
        sid = cur.fetchone()["id"]
    conn.commit()
    return sid


def finish_scan_session(conn, session_id: int, status: str,
                        total_files: int, total_folders: int,
                        new_f: int, upd_f: int, del_f: int,
                        elapsed: float, error: str = None):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE scan_sessions SET
                ended_at         = NOW(),
                status           = %s,
                total_files      = %s,
                total_folders    = %s,
                new_files        = %s,
                updated_files    = %s,
                deleted_files    = %s,
                duration_seconds = %s,
                error_message    = %s
            WHERE id = %s
        """, (status, total_files, total_folders,
              new_f, upd_f, del_f,
              int(elapsed), error, session_id))
        # Also stamp drive scanned_at
        conn.execute("""
            UPDATE drives SET scanned_at = NOW()
            WHERE id = (SELECT drive_id FROM scan_sessions WHERE id = %s)
        """, (session_id,))
    conn.commit()


# ── DB — batch file insert/update/delete ───────────────────────────────────────
def sync_files_to_db(conn, drive_id: int, session_id: int,
                     scanned: list[dict], output_queue: queue.Queue
                     ) -> tuple[int, int, int]:
    """
    Sync the freshly-scanned file list against the DB.
    Returns (new_count, updated_count, deleted_count).
    """
    output_queue.put({"type": "db_status", "msg": "Syncing to database…"})

    # ── Fetch existing paths for this drive ─────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, path, modified_at, size_bytes FROM files WHERE drive_id = %s AND is_missing = FALSE",
            (drive_id,)
        )
        existing: dict[str, dict] = {r["path"]: r for r in cur.fetchall()}

    scanned_paths = {r["path"] for r in scanned}

    # ── Hard-delete removed files ────────────────────────────────────────────
    removed_paths = set(existing.keys()) - scanned_paths
    deleted_count = 0
    if removed_paths:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM files WHERE drive_id = %s AND path = ANY(%s)",
                (drive_id, list(removed_paths))
            )
            deleted_count = cur.rowcount
        conn.commit()

    # ── Upsert scanned files ─────────────────────────────────────────────────
    new_count = updated_count = 0
    INSERT_BATCH = 500

    rows_to_insert: list[dict] = []
    rows_to_update: list[dict] = []

    for rec in scanned:
        path = rec["path"]
        if path not in existing:
            new_count += 1
            rows_to_insert.append(rec)
        else:
            old = existing[path]
            # Only update if size or modified_at changed
            old_mod = old["modified_at"]
            new_mod = rec.get("modified_at")
            if (old["size_bytes"] != rec.get("size_bytes") or
                    (new_mod and old_mod and
                     new_mod.replace(tzinfo=None) != old_mod.replace(tzinfo=None))):
                updated_count += 1
                rec["existing_id"] = old["id"]
                rows_to_update.append(rec)

    # Batch insert new files
    for i in range(0, len(rows_to_insert), INSERT_BATCH):
        batch = rows_to_insert[i:i + INSERT_BATCH]
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO files
                    (drive_id, scan_session_id, path, name, extension,
                     is_dir, size_bytes, size_display, modified_at,
                     created_at_fs, accessed_at_fs, depth, last_scan_id)
                VALUES
                    (%(drive_id)s, %(session_id)s, %(path)s, %(name)s, %(extension)s,
                     %(is_dir)s, %(size_bytes)s, %(size_display)s, %(modified_at)s,
                     %(created_at_fs)s, %(accessed_at_fs)s, %(depth)s, %(session_id)s)
                ON CONFLICT (drive_id, path) DO NOTHING
            """, [{**r, "drive_id": drive_id, "session_id": session_id} for r in batch])
        conn.commit()

    # Batch update changed files
    for rec in rows_to_update:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE files SET
                    size_bytes     = %(size_bytes)s,
                    size_display   = %(size_display)s,
                    modified_at    = %(modified_at)s,
                    accessed_at_fs = %(accessed_at_fs)s,
                    last_seen_at   = NOW(),
                    last_scan_id   = %(session_id)s,
                    is_missing     = FALSE
                WHERE id = %(existing_id)s
            """, {**rec, "session_id": session_id})
        conn.commit()

    # ── Insert hashes for new/updated files ─────────────────────────────────
    changed_records = rows_to_insert + rows_to_update
    output_queue.put({"type": "db_status",
                      "msg": f"Hashing {len(changed_records)} files…"})

    for rec in changed_records:
        if rec.get("is_dir") or not rec.get("abs_path"):
            continue
        hashes = compute_hashes(Path(rec["abs_path"]))
        if not hashes:
            continue
        # Fetch the file id
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM files WHERE drive_id = %s AND path = %s",
                (drive_id, rec["path"])
            )
            row = cur.fetchone()
            if not row:
                continue
            fid = row["id"]
            for htype, hval in hashes.items():
                cur.execute("""
                    INSERT INTO file_hashes (file_id, hash_type, hash_value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (file_id, hash_type) DO UPDATE
                        SET hash_value = EXCLUDED.hash_value,
                            computed_at = NOW()
                """, (fid, htype, hval))
        conn.commit()

    output_queue.put({"type": "db_status", "msg": "Database sync complete."})
    return new_count, updated_count, deleted_count


# ── File scanning ──────────────────────────────────────────────────────────────
def _size_display(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    else:
        return f"{size_bytes / (1024 ** 3):.2f} GB"


def scan_drive(drive: str, stop_event: threading.Event,
               pause_event: threading.Event, output_queue: queue.Queue,
               max_depth: int = 8):
    """
    Full scan of a USB drive. Collects all file metadata in-memory,
    then batch-syncs to PostgreSQL at the end.
    """
    pythoncom.CoInitialize()
    root = Path(drive)
    scanned: list[dict] = []
    file_count = folder_count = 0
    start_time = time.time()

    output_queue.put({"type": "progress", "action": "start"})

    for item in root.rglob("*"):
        if stop_event and stop_event.is_set():
            break
        while pause_event and pause_event.is_set():
            time.sleep(0.2)

        try:
            rel   = str(item.relative_to(root))
            depth = len(Path(rel).parts)
        except Exception:
            continue

        if depth > max_depth:
            continue

        try:
            stat     = item.stat()
            mod_dt   = datetime.fromtimestamp(stat.st_mtime)
            crt_dt   = datetime.fromtimestamp(stat.st_ctime)
            acc_dt   = datetime.fromtimestamp(stat.st_atime)
            mod_str  = mod_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            stat = mod_dt = crt_dt = acc_dt = None
            mod_str = ""

        name      = item.name
        ext       = item.suffix.lower().lstrip(".") if item.suffix else None
        is_dir    = item.is_dir()

        if is_dir:
            folder_count += 1
            size_bytes   = None
            size_disp    = ""
        else:
            file_count  += 1
            size_bytes   = stat.st_size if stat else None
            size_disp    = _size_display(size_bytes) if size_bytes is not None else ""

        rec = {
            "path":         rel,
            "name":         name,
            "extension":    ext,
            "is_dir":       is_dir,
            "size_bytes":   size_bytes,
            "size_display": size_disp,
            "modified_at":  mod_dt,
            "created_at_fs": crt_dt,
            "accessed_at_fs": acc_dt,
            "depth":        depth,
            "abs_path":     str(item),
        }
        scanned.append(rec)

        # Stream to GUI for live tree rendering
        output_queue.put({
            "type":   "node",
            "path":   rel,
            "is_dir": is_dir,
            "date":   mod_str,
            "size":   size_disp,
        })

    elapsed = round(time.time() - start_time, 2)

    output_queue.put({
        "type": "summary",
        "files":   file_count,
        "folders": folder_count,
        "time":    elapsed,
    })

    # ── DB sync ──────────────────────────────────────────────────────────────
    try:
        conn     = get_conn()
        drive_id, info = upsert_drive(conn, drive)
        session_id     = start_scan_session(conn, drive_id)

        new_f, upd_f, del_f = sync_files_to_db(
            conn, drive_id, session_id, scanned, output_queue
        )

        status = "STOPPED" if (stop_event and stop_event.is_set()) else "COMPLETED"
        finish_scan_session(conn, session_id, status,
                            file_count, folder_count,
                            new_f, upd_f, del_f, elapsed)
        conn.close()

        output_queue.put({
            "type":     "db_done",
            "drive_id": drive_id,
            "new":      new_f,
            "updated":  upd_f,
            "deleted":  del_f,
        })
    except Exception as e:
        output_queue.put({"type": "db_error", "msg": str(e)})

    output_queue.put({"type": "progress", "action": "done"})
    output_queue.put({"type": "done"})


def on_drive_connected(drive: str, stop_event: threading.Event,
                       pause_event: threading.Event, output_queue: queue.Queue):
    time.sleep(1.5)  # Short settle delay

    wmi_uuid = get_drive_wmi_uuid(drive)
    serial   = get_volume_serial(drive)
    info     = get_drive_info(drive)

    output_queue.put({
        "type":     "root",
        "drive":    drive,
        "wmi_uuid": wmi_uuid or "",
        "serial":   serial or "",
        "label":    info["label"],
        "total":    info["total_gb"],
        "used":     info["used_gb"],
    })

    scan_drive(drive, stop_event, pause_event, output_queue)


def monitor_drives(poll_interval: float = 1.5,
                   stop_event: threading.Event = None,
                   output_queue: queue.Queue = None,
                   pause_event: threading.Event = None):
    """
    Main monitoring loop. Detects newly connected USB drives and
    spawns a scan thread per drive.
    """
    set_all_drives_offline()
    previous = get_connected_usb_drives()

    for d in previous:
        threading.Thread(
            target=on_drive_connected,
            args=(d, stop_event, pause_event, output_queue),
            daemon=True,
            ).start()
    while not (stop_event and stop_event.is_set()):
        time.sleep(poll_interval)
        current = get_connected_usb_drives()
        added   = current - previous

        for d in added:
            threading.Thread(
                 target=on_drive_connected,
                 args=(d, stop_event, pause_event, output_queue),
                 daemon=True,
            ).start()

        # Mark removed drives offline
        removed = previous - current
        for d in removed:
            wmi_uuid = get_drive_wmi_uuid(d)
            if wmi_uuid:
                mark_drive_offline(wmi_uuid)
            output_queue.put({"type": "drive_offline", "drive": d})

        previous = current


# ── Offline DB queries (Phase 1 — search unplugged drives) ────────────────────
def get_all_drives_from_db() -> list[dict]:
    """Return all drives ever seen, with online status."""
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT d.*, ss.total_files, ss.total_folders,
                       ss.started_at AS last_scan_at, ss.status AS last_scan_status
                FROM drives d
                LEFT JOIN LATERAL (
                    SELECT total_files, total_folders, started_at, status
                    FROM scan_sessions
                    WHERE drive_id = d.id
                    ORDER BY started_at DESC LIMIT 1
                ) ss ON TRUE
                ORDER BY d.is_online DESC, d.last_seen_at DESC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def get_files_for_drive(drive_id: int, query: str = "") -> list[dict]:
    """Return files for a drive from DB (works offline). Optionally filter."""
    try:
        with get_conn() as conn:
            if query:
                rows = conn.execute("""
                    SELECT * FROM search_files(%s, %s, 500)
                """, (query, drive_id)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT f.id, f.drive_id, d.drive_letter, d.label AS drive_label,
                           d.is_online, f.path, f.name, f.extension, f.is_dir,
                           f.size_display, f.modified_at, 0.0 AS rank
                    FROM files f
                    JOIN drives d ON d.id = f.drive_id
                    WHERE f.drive_id = %s AND f.is_missing = FALSE
                    ORDER BY f.path
                    LIMIT 5000
                """, (drive_id,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def search_all_drives(query: str) -> list[dict]:
    """Full-text search across all drives (online + offline)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM search_files(%s, NULL, 500)",
                (query,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []