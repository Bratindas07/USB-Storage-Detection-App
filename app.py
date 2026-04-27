import wmi
import time
import threading
import pythoncom
from datetime import datetime
from pathlib import Path


def get_connected_drives():
    pythoncom.CoInitialize()
    c = wmi.WMI()
    drives = set()

    for disk in c.Win32_DiskDrive():
        if disk.InterfaceType == "USB":
            for partition in disk.associators("Win32_DiskDriveToDiskPartition"):
                for logical in partition.associators("Win32_LogicalDiskToPartition"):
                    drives.add(logical.DeviceID + "\\")

    return drives


def get_drive_info(drive_letter):
    pythoncom.CoInitialize()
    c = wmi.WMI()
    drive = drive_letter.rstrip("\\")

    for d in c.Win32_LogicalDisk(DeviceID=drive):
        total = int(d.Size) if d.Size else 0
        free = int(d.FreeSpace) if d.FreeSpace else 0
        used = total - free

        return {
            "label": d.VolumeName or "No Label",
            "filesystem": d.FileSystem or "Unknown",
            "total": round(total / (1024**3), 2),
            "used": round(used / (1024**3), 2),
            "free": round(free / (1024**3), 2),
        }

    return {}


def scan_and_print_files(drive, stop_event=None, output_queue=None, pause_event=None, max_depth=5):
    root = Path(drive)

    file_count = 0
    folder_count = 0
    start_time = time.time()

    output_queue.put({"type": "progress", "action": "start"})

    for item in root.rglob("*"):

        if stop_event and stop_event.is_set():
            break

        while pause_event and pause_event.is_set():
            time.sleep(0.2)

        try:
            rel = str(item.relative_to(root))
            depth = len(Path(rel).parts)
        except:
            continue

        if depth > max_depth:
            continue

        try:
            stat = item.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except:
            modified = ""

        if item.is_dir():
            folder_count += 1
            output_queue.put({
                "type": "node",
                "path": rel,
                "is_dir": True,
                "date": modified,
                "size": ""
            })
            continue

        if item.is_file():
            size = stat.st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024**2:
                size_str = f"{size/1024:.1f} KB"
            elif size < 1024**3:
                size_str = f"{size/(1024**2):.1f} MB"
            else:
                size_str = f"{size/(1024**3):.2f} GB"

            file_count += 1

            output_queue.put({
                "type": "node",
                "path": rel,
                "is_dir": False,
                "date": modified,
                "size": size_str
            })

    total_time = round(time.time() - start_time, 2)

    output_queue.put({
        "type": "summary",
        "files": file_count,
        "folders": folder_count,
        "time": total_time
    })

    output_queue.put({"type": "progress", "action": "done"})


def on_drive_connected(drive, stop_event=None, output_queue=None, pause_event=None):
    time.sleep(2)

    info = get_drive_info(drive)

    output_queue.put({
        "type": "root",
        "drive": drive,
        "label": info["label"],
        "total": info["total"],
        "used": info["used"]
    })

    scan_and_print_files(drive, stop_event, output_queue, pause_event)


def monitor_drives(poll_interval=1.5, stop_event=None, output_queue=None, pause_event=None):
    previous = get_connected_drives()

    for d in previous:
        threading.Thread(target=on_drive_connected, args=(d, stop_event, output_queue, pause_event), daemon=True).start()

    while not (stop_event and stop_event.is_set()):
        time.sleep(poll_interval)

        current = get_connected_drives()
        added = current - previous

        for d in added:
            threading.Thread(target=on_drive_connected, args=(d, stop_event, output_queue, pause_event), daemon=True).start()

        previous = current