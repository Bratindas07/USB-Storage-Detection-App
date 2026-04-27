"""
USB Monitor — GUI (gui.py)  ·  Phase 0 + Phase 1
Cyberpunk Edition · CustomTkinter

New in Phase 1:
  - Multi-drive sidebar: active drives (green dot) + history button
  - Offline drive browser panel (loads tree from DB)
  - DB connection status indicator
  - DB sync stats overlay (new / updated / deleted)
  - Offline keyword search across all drives
  - Drive detail panel shows WMI UUID + serial
"""

import threading
import tkinter as tk
from tkinter import ttk, filedialog
import customtkinter as ctk
import queue
import os
import csv
import app as backend

# ── Palette ────────────────────────────────────────────────────────────────────
BG       = "#070b10"
SURFACE  = "#0c1018"
SURFACE2 = "#111722"
CARD     = "#0e1520"
BORDER   = "#0d2818"
GREEN    = "#00ff88"
BLUE     = "#00ccff"
PURPLE   = "#aa66ff"
DIM      = "#3a4a5a"
TEXT     = "#ccd6e0"
DANGER   = "#ff3b5c"
WARNING  = "#ffaa00"
OFFLINE  = "#556677"
MONO     = "Consolas"

ctk.set_appearance_mode("dark")


# ── Donut chart ────────────────────────────────────────────────────────────────
class DonutChart(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=CARD, highlightthickness=0, **kw)
        self._used = 0.0
        self._total = 1.0
        self._drive = ""
        self.bind("<Configure>", lambda _e: self.draw())

    def set_data(self, used, total, drive=""):
        self._used  = max(used, 0.0)
        self._total = max(total, 0.01)
        self._drive = drive
        self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        cx, cy = w // 2, h // 2
        r     = min(cx, cy) - 10
        thick = max(14, r // 3)
        pct   = min(self._used / self._total, 1.0)
        extent = pct * 359.9

        self.create_arc(cx-r, cy-r, cx+r, cy+r, start=0, extent=359.9,
                        style="arc", outline="#131f14", width=thick)
        if extent > 0:
            color = DANGER if pct > 0.9 else WARNING if pct > 0.7 else GREEN
            self.create_arc(cx-r, cy-r, cx+r, cy+r,
                            start=90, extent=-extent,
                            style="arc", outline=color, width=thick)
            self.create_arc(cx-r, cy-r, cx+r, cy+r,
                            start=90, extent=-extent,
                            style="arc", outline="#aaffcc",
                            width=max(2, thick // 5))

        color   = DANGER if pct > 0.9 else WARNING if pct > 0.7 else GREEN
        pct_int = int(pct * 100)
        self.create_text(cx, cy - 8, text=f"{pct_int}%",
                         fill=color, font=(MONO, 17, "bold"))
        self.create_text(cx, cy + 11,
                         text=f"{self._used:.1f} / {self._total:.1f} GB",
                         fill=DIM, font=(MONO, 8))
        if self._drive:
            self.create_text(cx, cy + r + 16, text=self._drive,
                             fill=DIM, font=(MONO, 8, "bold"))


# ── Drive row widget (sidebar) ─────────────────────────────────────────────────
class DriveRow(ctk.CTkFrame):
    def __init__(self, parent, drive_info: dict, on_click, **kw):
        super().__init__(parent, fg_color=SURFACE2, corner_radius=6, **kw)
        self.drive_info = drive_info
        self._selected = False

        is_online = drive_info.get("is_online", False)
        dot_color = GREEN if is_online else OFFLINE
        label     = drive_info.get("label") or "No Label"
        letter    = drive_info.get("drive_letter") or "?"
        files     = drive_info.get("total_files") or 0

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(6, 2))

        ctk.CTkLabel(top, text="●", font=ctk.CTkFont(MONO, 11),
                     text_color=dot_color).pack(side="left")
        ctk.CTkLabel(top, text=f"  {letter}  {label}",
                     font=ctk.CTkFont(MONO, 10, "bold"),
                     text_color=TEXT if is_online else DIM,
                     anchor="w").pack(side="left", fill="x", expand=True)

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(fill="x", padx=8, pady=(0, 6))
        status = "ONLINE" if is_online else "OFFLINE"
        s_col  = GREEN if is_online else OFFLINE
        ctk.CTkLabel(bot, text=status,
                     font=ctk.CTkFont(MONO, 8), text_color=s_col).pack(side="left")
        if files:
            ctk.CTkLabel(bot, text=f"  {files:,} files",
                         font=ctk.CTkFont(MONO, 8), text_color=DIM).pack(side="left")

        self.bind("<Button-1>", lambda e: on_click(drive_info))
        for child in self.winfo_children():
            child.bind("<Button-1>", lambda e: on_click(drive_info))
            for gc in child.winfo_children():
                gc.bind("<Button-1>", lambda e: on_click(drive_info))

    def set_selected(self, sel: bool):
        self._selected = sel
        self.configure(fg_color="#0a2818" if sel else SURFACE2)


# ── Main window ────────────────────────────────────────────────────────────────
class USBMonitorGUI(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("USB MONITOR // CYBER EDITION")
        self.geometry("1280x780")
        self.minsize(1024, 640)
        self.configure(fg_color=BG)

        # State
        self.stop_event   = threading.Event()
        self.pause_event  = threading.Event()
        self.queue        = queue.Queue()
        self.nodes:  dict[str, str] = {}
        self.all_data: list[dict]   = []
        self.root_node: str | None  = None
        self._root_text             = ""
        self._pulse_on              = True

        # Drive tracking
        self._drive_rows:  dict[str, DriveRow] = {}   # wmi_uuid → DriveRow
        self._active_drive_id: int | None = None       # currently viewed drive_id
        self._showing_history = False

        self._setup_tree_style()
        self._build_ui()
        self._check_db_connection()
        self._refresh_drive_sidebar()
        self._poll()

    # ── Tree style ─────────────────────────────────────────────────────────────
    def _setup_tree_style(self):
        s = ttk.Style()
        s.theme_use("default")
        s.configure("Cyber.Treeview",
            background=SURFACE, foreground=TEXT,
            fieldbackground=SURFACE, borderwidth=0,
            font=(MONO, 10), rowheight=26)
        s.configure("Cyber.Treeview.Heading",
            background=SURFACE2, foreground=GREEN,
            font=(MONO, 10, "bold"), relief="flat")
        s.map("Cyber.Treeview",
            background=[("selected", "#0a2818")],
            foreground=[("selected", GREEN)])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"Cyber.{orient}.TScrollbar",
                troughcolor=SURFACE2, background=DIM,
                arrowcolor=GREEN, borderwidth=0)

    # ── UI ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_titlebar()
        self._build_body()
        self._build_statusbar()

    def _build_titlebar(self):
        bar = ctk.CTkFrame(self, fg_color=SURFACE2, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        ctk.CTkLabel(bar, text="⬡  USB MONITOR",
                     font=ctk.CTkFont(MONO, 19, "bold"),
                     text_color=GREEN).pack(side="left", padx=20)

        tk.Frame(bar, bg=BORDER, width=2).pack(side="left", fill="y", pady=10)

        self._status_dot = ctk.CTkLabel(bar, text="●",
                                         font=ctk.CTkFont(MONO, 22), text_color=DIM)
        self._status_dot.pack(side="left", padx=(14, 4))
        self._status_lbl = ctk.CTkLabel(bar, text="OFFLINE",
                                         font=ctk.CTkFont(MONO, 11, "bold"),
                                         text_color=DIM)
        self._status_lbl.pack(side="left")

        # DB status pill
        self._db_lbl = ctk.CTkLabel(bar, text="DB ●",
                                     font=ctk.CTkFont(MONO, 9, "bold"),
                                     text_color=DIM)
        self._db_lbl.pack(side="right", padx=(0, 6))
        ctk.CTkLabel(bar, text="DATABASE:",
                     font=ctk.CTkFont(MONO, 9), text_color=DIM).pack(side="right")

        self._summary_lbl = ctk.CTkLabel(bar, text="",
                                          font=ctk.CTkFont(MONO, 10), text_color=DIM)
        self._summary_lbl.pack(side="right", padx=20)

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        body.columnconfigure(0, weight=0, minsize=230)
        body.columnconfigure(1, weight=0, minsize=200)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_left_sidebar(body)
        self._build_drive_panel(body)
        self._build_main_panel(body)

    # ── Left sidebar (controls + stats) ────────────────────────────────────────
    def _build_left_sidebar(self, parent):
        sb = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8, width=230)
        sb.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        sb.pack_propagate(False)

        self._section_label(sb, "STORAGE")
        self.donut = DonutChart(sb, width=170, height=170)
        self.donut.pack(pady=(2, 10))

        self._divider(sb)
        self._section_label(sb, "CONTROLS")
        btns = ctk.CTkFrame(sb, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(4, 10))

        self.start_btn = self._cyber_btn(btns, "▶  START", "#003d1a", "#005525", GREEN, self.start)
        self.start_btn.pack(fill="x", pady=(0, 5))
        self.stop_btn  = self._cyber_btn(btns, "■  STOP",  "#2a0010", "#3d0018", DANGER, self.stop)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(fill="x", pady=(0, 5))
        self.pause_btn = self._cyber_btn(btns, "⏸  PAUSE", "#1c1500", "#2a2000", WARNING, self.pause)
        self.pause_btn.configure(state="disabled")
        self.pause_btn.pack(fill="x")

        self._divider(sb)
        self._section_label(sb, "STATISTICS")
        stats = ctk.CTkFrame(sb, fg_color=SURFACE, corner_radius=6)
        stats.pack(fill="x", padx=12, pady=(4, 4))
        self._s_files   = self._stat_row(stats, "FILES",   "—")
        self._s_folders = self._stat_row(stats, "DIRS",    "—")
        self._s_time    = self._stat_row(stats, "TIME",    "—")

        # DB sync stats
        db_stats = ctk.CTkFrame(sb, fg_color=SURFACE, corner_radius=6)
        db_stats.pack(fill="x", padx=12, pady=(4, 4))
        self._s_new     = self._stat_row(db_stats, "NEW",     "—", GREEN)
        self._s_updated = self._stat_row(db_stats, "UPD",     "—", WARNING)
        self._s_deleted = self._stat_row(db_stats, "DEL",     "—", DANGER)

        self._divider(sb)
        self._section_label(sb, "EXPORT")
        ctk.CTkButton(sb, text="⤓  EXPORT CSV",
                      font=ctk.CTkFont(MONO, 10),
                      fg_color="transparent", hover_color=SURFACE2,
                      text_color=BLUE, border_color=BLUE, border_width=1,
                      corner_radius=4, height=32,
                      command=self.export_csv).pack(fill="x", padx=12, pady=(4, 14))

    # ── Drive panel (middle column) ─────────────────────────────────────────────
    def _build_drive_panel(self, parent):
        panel = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8, width=200)
        panel.grid(row=0, column=1, sticky="nsew", padx=(0, 6))
        panel.pack_propagate(False)
        panel.columnconfigure(0, weight=1)

        # Header row
        hdr = ctk.CTkFrame(panel, fg_color=SURFACE2, corner_radius=0, height=40)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        self._drives_tab_lbl = ctk.CTkLabel(
            hdr, text="⚡ ACTIVE DRIVES",
            font=ctk.CTkFont(MONO, 10, "bold"), text_color=GREEN)
        self._drives_tab_lbl.pack(side="left", padx=10)

        self._history_btn = ctk.CTkButton(
            hdr, text="HISTORY",
            font=ctk.CTkFont(MONO, 8, "bold"),
            width=64, height=24,
            fg_color="transparent", hover_color=SURFACE,
            text_color=DIM, border_color=DIM, border_width=1, corner_radius=4,
            command=self._toggle_history)
        self._history_btn.pack(side="right", padx=8)

        # Scrollable drive list
        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0)
        scroll_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self._drive_list_frame = scroll_frame

        # Offline search bar
        self._divider(panel)
        ctk.CTkLabel(panel, text="⌕ OFFLINE SEARCH",
                     font=ctk.CTkFont(MONO, 9, "bold"), text_color=DIM).pack(pady=(4, 2))
        self._offline_var = tk.StringVar()
        self._offline_var.trace_add("write", self._on_offline_search)
        ctk.CTkEntry(panel, textvariable=self._offline_var,
                     placeholder_text="search all drives…",
                     font=ctk.CTkFont(MONO, 10),
                     fg_color=SURFACE, border_color=BORDER,
                     text_color=TEXT, placeholder_text_color=DIM,
                     height=30).pack(fill="x", padx=8, pady=(0, 8))

    def _toggle_history(self):
        self._showing_history = not self._showing_history
        if self._showing_history:
            self._history_btn.configure(text="ACTIVE", text_color=GREEN, border_color=GREEN)
            self._drives_tab_lbl.configure(text="🕑 DRIVE HISTORY", text_color=PURPLE)
        else:
            self._history_btn.configure(text="HISTORY", text_color=DIM, border_color=DIM)
            self._drives_tab_lbl.configure(text="⚡ ACTIVE DRIVES", text_color=GREEN)
        self._refresh_drive_sidebar()

    def _refresh_drive_sidebar(self):
        for w in self._drive_list_frame.winfo_children():
            w.destroy()
        self._drive_rows.clear()

        drives = backend.get_all_drives_from_db()

        if self._showing_history:
            show = [d for d in drives if not d.get("is_online")]
        else:
            show = [d for d in drives if d.get("is_online")]

        if not show:
            msg = "No history found." if self._showing_history else "No drives connected."
            ctk.CTkLabel(self._drive_list_frame, text=msg,
                         font=ctk.CTkFont(MONO, 9), text_color=DIM).pack(pady=20)
            return

        for d in show:
            uuid = d.get("wmi_uuid", "")
            row  = DriveRow(self._drive_list_frame, d,
                            on_click=self._on_drive_row_click)
            row.pack(fill="x", pady=(0, 4))
            self._drive_rows[uuid] = row

    def _on_drive_row_click(self, drive_info: dict):
        # Deselect all
        for row in self._drive_rows.values():
            row.set_selected(False)
        uuid = drive_info.get("wmi_uuid", "")
        if uuid in self._drive_rows:
            self._drive_rows[uuid].set_selected(True)

        drive_id = drive_info.get("id")
        self._active_drive_id = drive_id

        # Load from DB into tree
        self._load_drive_from_db(drive_info)

    def _load_drive_from_db(self, drive_info: dict):
        """Populate the file tree from DB (works for offline drives)."""
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.all_data.clear()
        self.root_node = None

        drive_id = drive_info.get("id")
        label    = drive_info.get("label") or "No Label"
        letter   = drive_info.get("drive_letter") or "?"
        total    = drive_info.get("total_gb") or 0
        used     = drive_info.get("used_gb") or 0
        is_on    = drive_info.get("is_online", False)
        status   = "ONLINE" if is_on else "OFFLINE"

        root_text = f"⬡  {letter}  [{label}]  {used:.1f} GB / {total:.1f} GB  [{status}]"
        self.root_node = self.tree.insert("", "end", text=root_text, open=True)
        self._root_text = root_text
        self.donut.set_data(used, total, letter)

        # Load files from DB in background
        def _load():
            files = backend.get_files_for_drive(drive_id)
            for f in files:
                self.queue.put({
                    "type":   "node",
                    "path":   f["path"],
                    "is_dir": f["is_dir"],
                    "date":   str(f["modified_at"])[:19] if f.get("modified_at") else "",
                    "size":   f.get("size_display") or "",
                })
            self.queue.put({"type": "db_load_done", "count": len(files)})

        threading.Thread(target=_load, daemon=True).start()
        self._set_status("LOADING DB", BLUE)

    # ── Main panel ─────────────────────────────────────────────────────────────
    def _build_main_panel(self, parent):
        panel = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=8)
        panel.grid(row=0, column=2, sticky="nsew")
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        # Search bar
        search_row = ctk.CTkFrame(panel, fg_color=SURFACE2, corner_radius=0, height=44)
        search_row.grid(row=0, column=0, sticky="ew")
        search_row.grid_propagate(False)

        ctk.CTkLabel(search_row, text="⌕",
                     font=ctk.CTkFont(MONO, 17), text_color=DIM).pack(side="left", padx=(14, 2))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search)
        ctk.CTkEntry(search_row, textvariable=self._search_var,
                     placeholder_text="filter file paths…",
                     font=ctk.CTkFont(MONO, 11),
                     fg_color="transparent", border_width=0,
                     text_color=TEXT, placeholder_text_color=DIM
                     ).pack(side="left", fill="both", expand=True, padx=(0, 14), pady=8)
        ctk.CTkButton(search_row, text="✕", width=30, height=26,
                      font=ctk.CTkFont(MONO, 11),
                      fg_color="transparent", hover_color=SURFACE,
                      text_color=DIM, corner_radius=4,
                      command=lambda: self._search_var.set("")
                      ).pack(side="right", padx=(0, 10))

        # Treeview
        tree_frame = tk.Frame(panel, bg=SURFACE)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        ys = ttk.Scrollbar(tree_frame, orient="vertical",   style="Cyber.Vertical.TScrollbar")
        xs = ttk.Scrollbar(tree_frame, orient="horizontal", style="Cyber.Horizontal.TScrollbar")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")

        self.tree = ttk.Treeview(
            tree_frame, style="Cyber.Treeview",
            columns=("Name", "Modified", "Size"),
            show="tree headings",
            yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.config(command=self.tree.yview)
        xs.config(command=self.tree.xview)

        self.tree.heading("#0",       text="  PATH STRUCTURE")
        self.tree.heading("Name",     text="NAME")
        self.tree.heading("Modified", text="MODIFIED")
        self.tree.heading("Size",     text="SIZE")
        self.tree.column("#0",       width=340, minwidth=200)
        self.tree.column("Name",     width=160, minwidth=80)
        self.tree.column("Modified", width=160, minwidth=100)
        self.tree.column("Size",     width=100, minwidth=60)

        self.tree.tag_configure("dir",     foreground=BLUE)
        self.tree.tag_configure("file",    foreground=TEXT)
        self.tree.tag_configure("offline", foreground=OFFLINE)

        # Context menu
        self._ctx_menu = tk.Menu(self, tearoff=0,
            bg=SURFACE2, fg=TEXT,
            activebackground="#0a2818", activeforeground=GREEN,
            font=(MONO, 10), borderwidth=1, relief="solid")
        self._ctx_menu.add_command(label="  📂  Open in Explorer",  command=self._ctx_open)
        self._ctx_menu.add_command(label="  ⎘   Copy Full Path",    command=self._ctx_copy_path)
        self._ctx_menu.add_command(label="  📄  Copy File Name",    command=self._ctx_copy_name)
        self._ctx_menu.add_separator()
        self._ctx_menu.add_command(label="  ✕   Dismiss",           command=lambda: None)
        self.tree.bind("<Button-3>", self._on_right_click)

    # ── Status bar ─────────────────────────────────────────────────────────────
    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color=SURFACE2, corner_radius=0, height=32)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._prog_lbl = ctk.CTkLabel(bar, text="READY",
                                       font=ctk.CTkFont(MONO, 9), text_color=DIM)
        self._prog_lbl.pack(side="left", padx=14)

        self._db_msg_lbl = ctk.CTkLabel(bar, text="",
                                         font=ctk.CTkFont(MONO, 9), text_color=BLUE)
        self._db_msg_lbl.pack(side="right", padx=14)

        self.progress = ctk.CTkProgressBar(bar, mode="indeterminate",
                                            fg_color=SURFACE, progress_color=GREEN, height=3)
        self.progress.pack(fill="x", side="bottom")

    # ── Helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    def _cyber_btn(parent, text, bg, hover, color, cmd):
        return ctk.CTkButton(parent, text=text,
                             font=ctk.CTkFont(MONO, 11, "bold"),
                             fg_color=bg, hover_color=hover,
                             text_color=color, border_color=color, border_width=1,
                             corner_radius=4, height=36, command=cmd)

    @staticmethod
    def _section_label(parent, text):
        ctk.CTkLabel(parent, text=text,
                     font=ctk.CTkFont(MONO, 9, "bold"), text_color=DIM).pack(pady=(10, 2))

    @staticmethod
    def _divider(parent):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", padx=12, pady=4)

    @staticmethod
    def _stat_row(parent, label, value, val_color=GREEN):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=3)
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(MONO, 9),
                     text_color=DIM, width=46, anchor="w").pack(side="left")
        lbl = ctk.CTkLabel(row, text=value, font=ctk.CTkFont(MONO, 11, "bold"),
                           text_color=val_color, anchor="e")
        lbl.pack(side="right")
        return lbl

    # ── DB check ───────────────────────────────────────────────────────────────
    def _check_db_connection(self):
        def _check():
            ok = backend.test_connection()
            color = GREEN if ok else DANGER
            text  = "DB ●  CONNECTED" if ok else "DB ●  OFFLINE"
            self.after(0, lambda: self._db_lbl.configure(text=text, text_color=color))
        threading.Thread(target=_check, daemon=True).start()

    # ── Controls ───────────────────────────────────────────────────────────────
    def start(self):
        self.stop_event.clear()
        self.pause_event.clear()
        self.nodes.clear()
        self.all_data.clear()
        self.root_node = None
        self._root_text = ""
        self.tree.delete(*self.tree.get_children())
        self._search_var.set("")
        self._active_drive_id = None

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(state="normal")
        self._set_status("SCANNING", GREEN)
        self._prog_lbl.configure(text="SCANNING DRIVE…")
        self.donut.set_data(0, 1, "")

        threading.Thread(
            target=backend.monitor_drives,
            kwargs={"stop_event": self.stop_event,
                    "pause_event": self.pause_event,
                    "output_queue": self.queue},
            daemon=True).start()
        self._pulse()

    def stop(self):
        self.stop_event.set()
        self.progress.stop()
        self._set_status("STOPPED", DANGER)
        self._prog_lbl.configure(text="SCAN ABORTED")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")

    def pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="⏸  PAUSE")
            self._set_status("SCANNING", GREEN)
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="▶  RESUME")
            self._set_status("PAUSED", WARNING)

    def export_csv(self):
        if not self.all_data:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
            title="Export USB file list")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Path", "Type", "Modified", "Size"])
            for d in self.all_data:
                w.writerow([d.get("path", ""),
                            "DIR" if d.get("is_dir") else "FILE",
                            d.get("date", ""), d.get("size", "")])

    # ── Tree insertion ──────────────────────────────────────────────────────────
    def insert_node(self, path, is_dir, date="", size=""):
        self.all_data.append({"path": path, "is_dir": is_dir, "date": date, "size": size})
        q = self._search_var.get().lower().strip()
        if q and q not in path.lower():
            return
        self._insert_node_raw(path, is_dir, date, size)

    def _insert_node_raw(self, path, is_dir, date="", size=""):
        sep    = "\\" if "\\" in path else "/"
        parts  = path.replace("/", "\\").split("\\")
        parent = self.root_node or ""
        full   = ""

        for i, part in enumerate(parts):
            full    = (full + "\\" + part) if full else part
            is_last = i == len(parts) - 1

            if full not in self.nodes:
                node_dir = (not is_last) or is_dir
                tag  = "dir" if node_dir else "file"
                icon = "📁" if node_dir else "📄"
                iid  = self.tree.insert(
                    parent or "", "end",
                    text=f"  {icon} {part}",
                    values=(part,
                            date if is_last else "",
                            size if is_last else ""),
                    tags=(tag,))
                self.nodes[full] = iid
            parent = self.nodes[full]

    # ── Search ──────────────────────────────────────────────────────────────────
    def _on_search(self, *_):
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.root_node = None
        if self._root_text:
            self.root_node = self.tree.insert("", "end", text=self._root_text, open=True)
        q = self._search_var.get().lower().strip()
        for d in self.all_data:
            if not q or q in d["path"].lower():
                self._insert_node_raw(d["path"], d["is_dir"],
                                      d.get("date", ""), d.get("size", ""))

    def _on_offline_search(self, *_):
        q = self._offline_var.get().strip()
        if len(q) < 2:
            return
        def _search():
            results = backend.search_all_drives(q)
            self.queue.put({"type": "offline_results", "results": results, "query": q})
        threading.Thread(target=_search, daemon=True).start()

    def _show_offline_results(self, results: list[dict], query: str):
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        self.root_node = None
        self._root_text = f"⌕  SEARCH: \"{query}\"  —  {len(results)} results"
        self.root_node = self.tree.insert("", "end", text=self._root_text, open=True)

        # Group by drive
        drives: dict[str, list] = {}
        for r in results:
            key = f"{r.get('drive_letter', '?')} [{r.get('drive_label', '')}]"
            drives.setdefault(key, []).append(r)

        for drive_key, files in drives.items():
            online = files[0].get("is_online", False)
            dot    = "⚡" if online else "○"
            diid   = self.tree.insert(
                self.root_node, "end",
                text=f"  {dot} {drive_key}",
                tags=("dir" if online else "offline",))
            for f in files:
                icon = "📁" if f.get("is_dir") else "📄"
                mod  = str(f.get("modified_at", ""))[:19]
                self.tree.insert(
                    diid, "end",
                    text=f"  {icon} {f.get('name', '')}",
                    values=(f.get("name", ""), mod, f.get("size_display", "")),
                    tags=("dir" if f.get("is_dir") else
                          ("file" if f.get("is_online") else "offline"),))

    # ── Context menu ────────────────────────────────────────────────────────────
    def _on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            try:
                self._ctx_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._ctx_menu.grab_release()

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        for path, node_iid in self.nodes.items():
            if node_iid == iid:
                return path
        return None

    def _ctx_open(self):
        path = self._selected_path()
        if path:
            target = path if os.path.isdir(path) else os.path.dirname(path)
            if os.path.exists(target):
                os.startfile(target)

    def _ctx_copy_path(self):
        path = self._selected_path()
        if path:
            self.clipboard_clear(); self.clipboard_append(path)

    def _ctx_copy_name(self):
        path = self._selected_path()
        if path:
            self.clipboard_clear(); self.clipboard_append(os.path.basename(path))

    # ── Status helpers ──────────────────────────────────────────────────────────
    def _set_status(self, text, color):
        self._status_lbl.configure(text=text, text_color=color)
        self._status_dot.configure(text_color=color)

    def _pulse(self):
        if not self.stop_event.is_set() and not self.pause_event.is_set():
            c = GREEN if self._pulse_on else "#004422"
            self._status_dot.configure(text_color=c)
            self._pulse_on = not self._pulse_on
            self.after(550, self._pulse)

    # ── Queue polling ────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True:
                msg  = self.queue.get_nowait()
                kind = msg.get("type")

                if kind == "progress":
                    if msg.get("action") == "start":
                        self.progress.start()
                    else:
                        self.progress.stop()

                elif kind == "root":
                    used  = float(msg.get("used",  0))
                    total = float(msg.get("total", 0))
                    drive = str(msg.get("drive",  ""))
                    label = str(msg.get("label",  ""))
                    self._root_text = (f"⬡  {drive}  [{label}]  "
                                       f"{used:.1f} GB / {total:.1f} GB")
                    self.root_node = self.tree.insert(
                        "", "end", text=self._root_text, open=True)
                    self.donut.set_data(used, total, drive)

                elif kind == "node":
                    self.insert_node(msg["path"], msg["is_dir"],
                                     msg.get("date", ""), msg.get("size", ""))

                elif kind == "summary":
                    files   = msg.get("files",   0)
                    folders = msg.get("folders", 0)
                    elapsed = msg.get("time",    "?")
                    self._summary_lbl.configure(
                        text=f"FILES:{files}  DIRS:{folders}  TIME:{elapsed}s")
                    self._s_files.configure(text=str(files))
                    self._s_folders.configure(text=str(folders))
                    self._s_time.configure(text=f"{elapsed}s")

                elif kind == "db_status":
                    self._db_msg_lbl.configure(text=msg.get("msg", ""))

                elif kind == "db_done":
                    self._s_new.configure(text=str(msg.get("new", 0)))
                    self._s_updated.configure(text=str(msg.get("updated", 0)))
                    self._s_deleted.configure(text=str(msg.get("deleted", 0)))
                    self._refresh_drive_sidebar()
                    self._db_msg_lbl.configure(text="✓ DB synced", )

                elif kind == "db_error":
                    self._db_msg_lbl.configure(
                        text=f"DB ERR: {msg.get('msg','')[:60]}")
                    self._db_lbl.configure(text="DB ●  ERROR", text_color=DANGER)

                elif kind == "db_load_done":
                    self._set_status("READY", GREEN)
                    self._prog_lbl.configure(
                        text=f"Loaded {msg.get('count', 0)} records from DB")

                elif kind == "drive_offline":
                    self._refresh_drive_sidebar()

                elif kind == "offline_results":
                    self._show_offline_results(
                        msg.get("results", []), msg.get("query", ""))

                elif kind == "done":
                    self.progress.stop()
                    self._set_status("COMPLETE", GREEN)
                    self._prog_lbl.configure(text="SCAN COMPLETE")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.pause_btn.configure(state="disabled")
                    self._refresh_drive_sidebar()

        except Exception:
            pass

        self.after(100, self._poll)


if __name__ == "__main__":
    app = USBMonitorGUI()
    app.mainloop()