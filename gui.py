import threading
import tkinter as tk
from tkinter import ttk
import queue
import app as backend


class USBMonitorGUI:

    def __init__(self, root):
        self.root = root
        self.root.title("USB Monitor")
        self.root.geometry("950x600")

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.queue = queue.Queue()

        self.nodes = {}
        self.root_node = None

        top = ttk.Frame(root)
        top.pack(fill="x")

        self.status = ttk.Label(top, text="OFF")
        self.status.pack(side="left")

        self.summary = ttk.Label(top, text="")
        self.summary.pack(side="right")

        btn = ttk.Frame(root)
        btn.pack()

        ttk.Button(btn, text="Start", command=self.start).grid(row=0, column=0)
        ttk.Button(btn, text="Stop", command=self.stop).grid(row=0, column=1)
        self.pause_btn = ttk.Button(btn, text="Pause", command=self.pause)
        self.pause_btn.grid(row=0, column=2)

        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x")

        frame = ttk.Frame(root)
        frame.pack(fill="both", expand=True)

        scroll = ttk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")

        self.tree = ttk.Treeview(
            frame,
            columns=("Name", "Modified", "Size"),
            show="tree headings",
            yscrollcommand=scroll.set
        )

        scroll.config(command=self.tree.yview)

        self.tree.heading("#0", text="Structure")
        self.tree.heading("Name", text="Name")
        self.tree.heading("Modified", text="Modified")
        self.tree.heading("Size", text="Size")

        self.tree.pack(fill="both", expand=True)

        self.poll()

    def start(self):
        self.stop_event.clear()
        self.pause_event.clear()
        self.nodes.clear()
        self.tree.delete(*self.tree.get_children())

        threading.Thread(
            target=backend.monitor_drives,
            kwargs={
                "stop_event": self.stop_event,
                "pause_event": self.pause_event,
                "output_queue": self.queue
            },
            daemon=True
        ).start()

        self.status.config(text="RUNNING")

    def stop(self):
        self.stop_event.set()
        self.progress.stop()
        self.status.config(text="STOPPED")

    def pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="Pause")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="Resume")

    def insert_node(self, path, is_dir, date="", size=""):
        parts = path.split("\\")
        parent = self.root_node
        full = ""

        for i, part in enumerate(parts):
            full = full + "\\" + part if full else part

            if full not in self.nodes:
                node = self.tree.insert(
                    parent,
                    "end",
                    text=part,
                    values=(part, date if i == len(parts)-1 else "", size if i == len(parts)-1 else "")
                )
                self.nodes[full] = node

            parent = self.nodes[full]

    def poll(self):
        try:
            while True:
                data = self.queue.get_nowait()

                if data["type"] == "progress":
                    if data["action"] == "start":
                        self.progress.start()
                    else:
                        self.progress.stop()

                elif data["type"] == "root":
                    text = f"{data['drive']} ({data['label']}) | {data['used']}GB / {data['total']}GB used"
                    self.root_node = self.tree.insert("", "end", text=text, open=True)

                elif data["type"] == "node":
                    self.insert_node(data["path"], data["is_dir"], data.get("date", ""), data.get("size", ""))

                elif data["type"] == "summary":
                    self.summary.config(
                        text=f"Files: {data['files']} | Folders: {data['folders']} | Time: {data['time']}s"
                    )

        except queue.Empty:
            pass

        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    app = USBMonitorGUI(root)
    root.mainloop()