"""Live GUI for the Siglent SDM3045X bench multimeter over USBTMC/VISA."""

import csv
import queue
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import pyvisa
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

RESOURCE = "USB0::0xF4EC::0x1203::SDM34HCD901181::INSTR"
POLL_INTERVAL_S = 0.5
HISTORY_LEN = 600  # 5 minutes at 0.5s

FUNCTIONS = {
    "DC Voltage": ("CONF:VOLT:DC", "V"),
    "AC Voltage": ("CONF:VOLT:AC", "V"),
    "DC Current": ("CONF:CURR:DC", "A"),
    "AC Current": ("CONF:CURR:AC", "A"),
    "Resistance (2W)": ("CONF:RES", "Ω"),
    "Resistance (4W)": ("CONF:FRES", "Ω"),
    "Frequency": ("CONF:FREQ", "Hz"),
    "Capacitance": ("CONF:CAP", "F"),
    "Continuity": ("CONF:CONT", "Ω"),
    "Diode": ("CONF:DIOD", "V"),
}


class DMMWorker(threading.Thread):
    """Polls the instrument on a background thread so the GUI never blocks."""

    def __init__(self, out_queue):
        super().__init__(daemon=True)
        self.out_queue = out_queue
        self.cmd_queue = queue.Queue()
        self._stop = threading.Event()
        self.inst = None
        self.idn = None

    def connect(self):
        rm = pyvisa.ResourceManager()
        self.inst = rm.open_resource(RESOURCE)
        self.inst.timeout = 3000
        self.idn = self.inst.query("*IDN?").strip()

    def set_function(self, scpi_cmd):
        self.cmd_queue.put(scpi_cmd)

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            self.connect()
            self.out_queue.put(("connected", self.idn))
        except Exception as exc:  # noqa: BLE001
            self.out_queue.put(("error", str(exc)))
            return

        while not self._stop.is_set():
            try:
                while True:
                    cmd = self.cmd_queue.get_nowait()
                    self.inst.write(cmd)
            except queue.Empty:
                pass

            try:
                value = float(self.inst.query("READ?"))
                self.out_queue.put(("reading", value))
            except Exception as exc:  # noqa: BLE001
                self.out_queue.put(("error", str(exc)))

            time.sleep(POLL_INTERVAL_S)

        try:
            self.inst.close()
        except Exception:  # noqa: BLE001
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SDM3045X Live")
        self.geometry("820x560")
        self.configure(bg="#111318")

        self.out_queue = queue.Queue()
        self.worker = None
        self.history = deque(maxlen=HISTORY_LEN)
        self.timestamps = deque(maxlen=HISTORY_LEN)
        self.logging_rows = []
        self.log_enabled = tk.BooleanVar(value=False)
        self.current_unit = "V"

        self._build_ui()
        self._start_worker()
        self.after(100, self._poll_queue)

    # -- UI ---------------------------------------------------------------
    def _build_ui(self):
        top = tk.Frame(self, bg="#111318")
        top.pack(fill="x", padx=16, pady=(12, 4))

        self.status_label = tk.Label(
            top, text="Connecting...", fg="#8a8f98", bg="#111318", font=("Segoe UI", 10)
        )
        self.status_label.pack(side="left")

        controls = tk.Frame(self, bg="#111318")
        controls.pack(fill="x", padx=16, pady=4)

        tk.Label(controls, text="Function:", fg="#e6e6e6", bg="#111318", font=("Segoe UI", 10)).pack(
            side="left"
        )
        self.func_var = tk.StringVar(value="DC Voltage")
        func_menu = ttk.Combobox(
            controls, textvariable=self.func_var, values=list(FUNCTIONS.keys()), state="readonly", width=18
        )
        func_menu.pack(side="left", padx=(6, 16))
        func_menu.bind("<<ComboboxSelected>>", self._on_function_change)

        self.log_check = tk.Checkbutton(
            controls,
            text="Log to CSV",
            variable=self.log_enabled,
            fg="#e6e6e6",
            bg="#111318",
            selectcolor="#1c1f26",
            activebackground="#111318",
            activeforeground="#e6e6e6",
        )
        self.log_check.pack(side="left")

        save_btn = tk.Button(controls, text="Save log...", command=self._save_log)
        save_btn.pack(side="left", padx=(10, 0))

        # Big digit readout
        readout_frame = tk.Frame(self, bg="#111318")
        readout_frame.pack(fill="x", padx=16, pady=(10, 0))

        self.value_label = tk.Label(
            readout_frame,
            text="--.----",
            fg="#5ec2ff",
            bg="#111318",
            font=("Consolas", 64, "bold"),
        )
        self.value_label.pack(side="left")

        self.unit_label = tk.Label(
            readout_frame, text="V", fg="#5ec2ff", bg="#111318", font=("Consolas", 28)
        )
        self.unit_label.pack(side="left", padx=(10, 0), pady=(20, 0), anchor="s")

        # Trend graph
        fig = Figure(figsize=(7.5, 3.2), dpi=100, facecolor="#111318")
        self.ax = fig.add_subplot(111)
        self.ax.set_facecolor("#181b22")
        self.ax.tick_params(colors="#8a8f98")
        for spine in self.ax.spines.values():
            spine.set_color("#2a2e38")
        self.ax.grid(True, color="#2a2e38", linewidth=0.6)
        (self.line,) = self.ax.plot([], [], color="#5ec2ff", linewidth=1.5)

        self.canvas = FigureCanvasTkAgg(fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(10, 16))

    # -- Worker plumbing ----------------------------------------------------
    def _start_worker(self):
        self.worker = DMMWorker(self.out_queue)
        self.worker.start()

    def _on_function_change(self, _event=None):
        name = self.func_var.get()
        cmd, unit = FUNCTIONS[name]
        self.current_unit = unit
        self.unit_label.config(text=unit)
        self.history.clear()
        self.timestamps.clear()
        if self.worker:
            self.worker.set_function(cmd)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.out_queue.get_nowait()
                if kind == "connected":
                    self.status_label.config(text=f"Connected: {payload}", fg="#5ec2ff")
                elif kind == "reading":
                    self._on_reading(payload)
                elif kind == "error":
                    self.status_label.config(text=f"Error: {payload}", fg="#ff6b6b")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_reading(self, value):
        now = time.time()
        self.history.append(value)
        self.timestamps.append(now)

        self.value_label.config(text=f"{value:.5f}")

        if self.log_enabled.get():
            self.logging_rows.append((datetime.now().isoformat(timespec="milliseconds"), value, self.current_unit))

        if len(self.history) >= 2:
            t0 = self.timestamps[0]
            xs = [t - t0 for t in self.timestamps]
            ys = list(self.history)
            self.line.set_data(xs, ys)
            self.ax.set_xlim(min(xs), max(xs) if max(xs) > 0 else 1)
            pad = (max(ys) - min(ys)) * 0.1 or abs(ys[-1] * 0.1) or 1
            self.ax.set_ylim(min(ys) - pad, max(ys) + pad)
            self.canvas.draw_idle()

    def _save_log(self):
        if not self.logging_rows:
            messagebox.showinfo("Save log", "No logged readings yet. Enable 'Log to CSV' first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"sdm3045x_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "value", "unit"])
            writer.writerows(self.logging_rows)
        messagebox.showinfo("Save log", f"Saved {len(self.logging_rows)} readings to {path}")

    def destroy(self):
        if self.worker:
            self.worker.stop()
        super().destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
