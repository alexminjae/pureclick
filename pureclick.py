from __future__ import annotations

import json
import platform
import csv
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from pureclick_core import (
    ClickError,
    KST,
    PureClickError,
    ServerClock,
    WindowsClicker,
    WindowsPrecision,
    parse_target_time,
    precise_wait_until,
)
from pureclick_seat_core import SeatPreferences, serialize_preferences


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return Path(__file__).resolve().parent


class PureClickApp(tk.Tk):
    SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp"
    SYNC_SAMPLES = 5
    FIRE_OFFSET_MS = 0.0
    PRE_MOVE_MS = 500
    CLICK_HOLD_MS = 0
    RETRY_CLICKS = 2
    RETRY_GAP_MS = 40

    def __init__(self) -> None:
        super().__init__()
        self.title("PureClick for Windows")
        self.geometry("680x620")
        self.minsize(620, 560)

        self.clock = ServerClock()
        self.clicker = WindowsClicker()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.deadline_perf: float | None = None
        self.target_server_unix: float | None = None

        today = datetime.now(KST)
        self.target_date = tk.StringVar(value=today.strftime("%Y-%m-%d"))
        self.target_hour = tk.StringVar(value=today.strftime("%H"))
        self.target_minute = tk.StringVar(value=today.strftime("%M"))
        self.target_second = tk.StringVar(value=today.strftime("%S"))
        self.target_millisecond = tk.StringVar(value="000")
        self.x_coord = tk.StringVar(value="")
        self.y_coord = tk.StringVar(value="")
        self.server_time_display = tk.StringVar(value="Syncing...")
        self.position_display = tk.StringVar(value="No click location selected")
        self.status = tk.StringVar(value="Idle")
        self.sync_info = tk.StringVar(value="Not synced")
        self.countdown = tk.StringVar(value="--")
        self.seat_grade_order = tk.StringVar(value="2,3,4,1")
        self.seat_max_attempts = tk.StringVar(value="80")
        self.seat_retry_ms = tk.StringVar(value="20")
        self.seat_poll_ms = tk.StringVar(value="40")
        self.seat_status = tk.StringVar(value="Seat autopilot not configured")

        self._load_seat_config()
        self._build_ui()
        self._tick_countdown()
        self._tick_server_time()
        self.after(200, self.sync_time)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root = ttk.Frame(self, padding=22)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text="Interpark Server Time", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(root, textvariable=self.server_time_display, font=("TkDefaultFont", 28, "bold")).grid(
            row=1, column=0, sticky="w", pady=(4, 2)
        )
        ttk.Label(root, text="poticket.interpark.com (booking server) · KST", foreground="#555555").grid(
            row=2, column=0, sticky="w"
        )

        form = ttk.Frame(root)
        form.grid(row=3, column=0, sticky="ew", pady=(24, 0))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Date").grid(row=0, column=0, sticky="w", pady=8, padx=(0, 14))
        ttk.Entry(form, textvariable=self.target_date, width=14, justify="center").grid(
            row=0, column=1, sticky="w", pady=8
        )

        ttk.Label(form, text="Click at").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 14))
        time_frame = ttk.Frame(form)
        time_frame.grid(row=1, column=1, sticky="w", pady=8)
        self._time_number(time_frame, "Hour", self.target_hour, 0, 23, 0, width=4)
        self._time_number(time_frame, "Min", self.target_minute, 0, 59, 2, width=4)
        self._time_number(time_frame, "Sec", self.target_second, 0, 59, 4, width=4)
        self._time_number(time_frame, "Ms", self.target_millisecond, 0, 999, 6, width=5)

        ttk.Label(form, text="Location").grid(row=2, column=0, sticky="w", pady=8, padx=(0, 14))
        location_frame = ttk.Frame(form)
        location_frame.grid(row=2, column=1, sticky="ew", pady=8)
        location_frame.columnconfigure(0, weight=1)
        ttk.Label(location_frame, textvariable=self.position_display).grid(row=0, column=0, sticky="w")
        self.capture_button = ttk.Button(
            location_frame,
            text="Lock Position",
            command=self.capture_cursor,
        )
        self.capture_button.grid(row=0, column=1, sticky="e", padx=(12, 0))

        button_frame = ttk.Frame(root)
        button_frame.grid(row=4, column=0, sticky="ew", pady=(24, 8))
        for column in range(4):
            button_frame.columnconfigure(column, weight=1)

        ttk.Button(button_frame, text="Sync Now", command=self.sync_time).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(button_frame, text="Test", command=lambda: self.arm(dry_run=True)).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        real_state = "normal" if platform.system() == "Windows" else "disabled"
        ttk.Button(
            button_frame,
            text="Arm",
            command=lambda: self.arm(dry_run=False),
            state=real_state,
        ).grid(row=0, column=2, sticky="ew", padx=8)
        ttk.Button(button_frame, text="Stop", command=self.stop).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )

        status_frame = ttk.LabelFrame(root, text="Status", padding=12)
        status_frame.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, text="State").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(status_frame, textvariable=self.status).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(status_frame, text="Countdown").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Label(status_frame, textvariable=self.countdown).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(status_frame, text="Sync").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Label(status_frame, textvariable=self.sync_info).grid(row=2, column=1, sticky="w", pady=4)

        seat_frame = ttk.LabelFrame(root, text="Phase 2 · Seat Autopilot (Onestop reserved shows)", padding=12)
        seat_frame.grid(row=6, column=0, sticky="ew", pady=(16, 0))
        seat_frame.columnconfigure(1, weight=1)

        ttk.Label(seat_frame, text="Grade order").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(seat_frame, textvariable=self.seat_grade_order).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Label(
            seat_frame,
            text="Comma-separated Interpark seat grades. Default 2,3,4,1 = R,S,A,OP.",
            foreground="#555555",
            wraplength=500,
        ).grid(row=1, column=0, columnspan=2, sticky="w")

        limits = ttk.Frame(seat_frame)
        limits.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(limits, text="Max tries").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(limits, textvariable=self.seat_max_attempts, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(limits, text="Retry ms").grid(row=0, column=2, sticky="w", padx=(16, 8))
        ttk.Entry(limits, textvariable=self.seat_retry_ms, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(limits, text="Poll ms").grid(row=0, column=4, sticky="w", padx=(16, 8))
        ttk.Entry(limits, textvariable=self.seat_poll_ms, width=8).grid(row=0, column=5, sticky="w")

        seat_buttons = ttk.Frame(seat_frame)
        seat_buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        seat_buttons.columnconfigure(0, weight=1)
        seat_buttons.columnconfigure(1, weight=1)
        ttk.Button(seat_buttons, text="Save Seat Config", command=self.save_seat_config).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(seat_buttons, text="Copy Userscript", command=self.copy_seat_script).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )
        ttk.Button(seat_buttons, text="Copy Config Snippet", command=self.copy_seat_config_snippet).grid(
            row=0, column=2, sticky="ew"
        )
        seat_buttons.columnconfigure(2, weight=1)
        ttk.Label(seat_frame, textvariable=self.seat_status, foreground="#555555", wraplength=520).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        notes = (
            "Phase 1: log in, select date/round, lock 예매하기, then arm for the exact KST server time. "
            "Phase 2: install the copied browser script once, then let it lock a seat on /onestop/seat "
            "while you finish payment manually."
        )
        ttk.Label(root, text=notes, foreground="#555555", wraplength=560).grid(
            row=7, column=0, sticky="w", pady=(14, 0)
        )

    def _time_number(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        from_: int,
        to: int,
        column: int,
        *,
        width: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w", padx=(0, 5))
        ttk.Spinbox(
            parent,
            from_=from_,
            to=to,
            textvariable=variable,
            width=width,
            justify="center",
            wrap=True,
        ).grid(row=0, column=column + 1, sticky="w", padx=(0, 12))

    def capture_cursor(self) -> None:
        self.capture_button.configure(state="disabled")
        self._capture_countdown(5)

    def _capture_countdown(self, seconds_left: int) -> None:
        if seconds_left > 0:
            self.position_display.set(f"Move your cursor now. Saving in {seconds_left}...")
            self.status.set("Move your cursor to the click location")
            self.after(1000, lambda: self._capture_countdown(seconds_left - 1))
            return

        try:
            x, y = self.clicker.cursor_position()
        except ClickError:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        self.x_coord.set(str(x))
        self.y_coord.set(str(y))
        self.position_display.set("Click location saved")
        self.status.set(f"Location saved at {x}, {y}")
        self.capture_button.configure(state="normal")

    def sync_time(self) -> None:
        self._start_worker(self._sync_worker)

    def arm(self, *, dry_run: bool) -> None:
        self._start_worker(lambda: self._arm_worker(dry_run=dry_run))

    def stop(self) -> None:
        self.stop_event.set()
        self.deadline_perf = None
        self.target_server_unix = None
        self.status.set("Stopped")

    def _start_worker(self, target) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("PureClick", "A task is already running.")
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _sync_worker(self) -> None:
        self._ui(lambda: self.status.set("Syncing server time..."))
        try:
            result = self._sync_now()
        except Exception as exc:
            message = str(exc)
            self._ui(lambda: self.status.set(f"Sync failed: {message}"))
            return

        self._show_sync_result(result)
        self._ui(lambda: self.status.set("Synced"))

    def _sync_now(self):
        return self.clock.sync_tick(
            self.SYNC_URL,
            sample_count=self.SYNC_SAMPLES,
            min_samples=2,
            max_wait_seconds=8.0,
            poll_seconds=0.005,
        )

    def _show_sync_result(self, result) -> None:
        info = (
            f"Synced ({result.mode}). Network {result.best_rtt_seconds * 1000:.0f} ms, "
            f"jitter {result.jitter_seconds * 1000:.0f} ms"
        )
        self._ui(lambda: self.sync_info.set(info))

    def _arm_worker(self, *, dry_run: bool) -> None:
        try:
            if not self.x_coord.get() or not self.y_coord.get():
                raise PureClickError("Lock a click position first")
            x = int(self.x_coord.get())
            y = int(self.y_coord.get())
            fire_offset_seconds = self.FIRE_OFFSET_MS / 1000.0
            pre_move_seconds = self.PRE_MOVE_MS / 1000.0

            self._ui(lambda: self.status.set("Fresh syncing before arm..."))
            result = self._sync_now()
            self._show_sync_result(result)

            target_unix = parse_target_time(
                self._target_time_text(),
                self.clock.server_time_unix(),
                target_tz=KST,
            )
            deadline_perf = self.clock.deadline_for_server_time(target_unix) + fire_offset_seconds
            if deadline_perf < time.perf_counter() - 0.100:
                raise PureClickError("Target time is already in the past")

            self.deadline_perf = deadline_perf
            self.target_server_unix = target_unix
            mode = "dry run" if dry_run else "real click"
            self._ui(lambda: self.status.set(f"Armed for {mode}"))

            if pre_move_seconds and not dry_run and platform.system() == "Windows":
                move_deadline = max(time.perf_counter(), deadline_perf - pre_move_seconds)
                if precise_wait_until(move_deadline, stop_event=self.stop_event):
                    self.clicker.move_to(x, y)

            final_wait_start = max(time.perf_counter(), deadline_perf - 2.0)
            if precise_wait_until(final_wait_start, stop_event=self.stop_event):
                with WindowsPrecision():
                    should_fire = precise_wait_until(deadline_perf, stop_event=self.stop_event)
            else:
                should_fire = False

            if not should_fire:
                self._ui(lambda: self.status.set("Stopped before target time"))
                return

            fired_perf = time.perf_counter()
            lateness_ms = (fired_perf - deadline_perf) * 1000
            if dry_run:
                self._ui(lambda: self.status.set(f"Dry run fired ({lateness_ms:+.2f} ms)"))
            else:
                self.clicker.click(x, y, hold_ms=self.CLICK_HOLD_MS)
                for _ in range(self.RETRY_CLICKS):
                    if self.stop_event.wait(self.RETRY_GAP_MS / 1000.0):
                        break
                    self.clicker.click(x, y, hold_ms=self.CLICK_HOLD_MS)
                self._ui(lambda: self.status.set(f"Clicked at {x}, {y} ({lateness_ms:+.2f} ms)"))
            self._write_fire_log(
                mode=mode,
                x=x,
                y=y,
                target_unix=target_unix,
                deadline_perf=deadline_perf,
                fired_perf=fired_perf,
                lateness_ms=lateness_ms,
            )
        except Exception as exc:
            message = str(exc)
            self._ui(lambda: self.status.set(f"Error: {message}"))
        finally:
            self.deadline_perf = None
            self.target_server_unix = None

    def _target_time_text(self) -> str:
        try:
            datetime.strptime(self.target_date.get().strip(), "%Y-%m-%d")
            hour = self._bounded_int(self.target_hour.get(), "Hour", 0, 23)
            minute = self._bounded_int(self.target_minute.get(), "Minute", 0, 59)
            second = self._bounded_int(self.target_second.get(), "Second", 0, 59)
            millisecond = self._bounded_int(
                self.target_millisecond.get(), "Millisecond", 0, 999
            )
        except ValueError as exc:
            raise PureClickError(str(exc)) from exc

        self.target_hour.set(f"{hour:02d}")
        self.target_minute.set(f"{minute:02d}")
        self.target_second.set(f"{second:02d}")
        self.target_millisecond.set(f"{millisecond:03d}")
        return (
            f"{self.target_date.get()} "
            f"{hour:02d}:{minute:02d}:{second:02d}.{millisecond:03d}"
        )

    @staticmethod
    def _bounded_int(value: str, label: str, minimum: int, maximum: int) -> int:
        text = value.strip()
        if not text.isdigit():
            raise ValueError(f"{label} must be a number")
        number = int(text)
        if number < minimum or number > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return number

    def _write_fire_log(
        self,
        *,
        mode: str,
        x: int,
        y: int,
        target_unix: float,
        deadline_perf: float,
        fired_perf: float,
        lateness_ms: float,
    ) -> None:
        result = self.clock.sync_result
        row = {
            "timestamp": datetime.now(KST).isoformat(timespec="milliseconds"),
            "mode": mode,
            "x": x,
            "y": y,
            "target_server_time": datetime.fromtimestamp(target_unix, KST).isoformat(
                timespec="milliseconds"
            ),
            "deadline_perf": f"{deadline_perf:.9f}",
            "fired_perf": f"{fired_perf:.9f}",
            "lateness_ms": f"{lateness_ms:.3f}",
            "offset_ms": f"{(result.offset_seconds * 1000):.3f}" if result else "",
            "best_rtt_ms": f"{(result.best_rtt_seconds * 1000):.3f}" if result else "",
            "jitter_ms": f"{(result.jitter_seconds * 1000):.3f}" if result else "",
        }
        path = Path(__file__).with_name("pureclick_fire_log.csv")
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _tick_countdown(self) -> None:
        if self.deadline_perf is None:
            self.countdown.set("--")
        else:
            remaining = self.deadline_perf - time.perf_counter()
            self.countdown.set(f"{max(0.0, remaining):.3f}s")
        self.after(50, self._tick_countdown)

    def _tick_server_time(self) -> None:
        result = self.clock.sync_result
        if result is None:
            self.server_time_display.set("Syncing...")
        else:
            server_time = datetime.fromtimestamp(self.clock.server_time_unix(), KST)
            millis = server_time.microsecond // 1000
            self.server_time_display.set(server_time.strftime("%Y-%m-%d %H:%M:%S") + f".{millis:03d}")
        self.after(50, self._tick_server_time)

    def _ui(self, callback) -> None:
        self.after(0, callback)

    def _seat_preferences(self) -> SeatPreferences:
        grades = [part.strip() for part in self.seat_grade_order.get().split(",") if part.strip()]
        return SeatPreferences.from_mapping(
            {
                "grade_order": grades,
                "max_attempts": self.seat_max_attempts.get().strip(),
                "retry_ms": self.seat_retry_ms.get().strip(),
                "poll_ms": self.seat_poll_ms.get().strip(),
            }
        )

    def _seat_config_path(self) -> Path:
        return app_dir().joinpath("pureclick_seat_config.json")

    def _load_seat_config(self) -> None:
        path = self._seat_config_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            preferences = SeatPreferences.from_mapping(data)
            self.seat_grade_order.set(",".join(preferences.grade_order))
            self.seat_max_attempts.set(str(preferences.max_attempts))
            self.seat_retry_ms.set(str(preferences.retry_ms))
            self.seat_poll_ms.set(str(preferences.poll_ms))
            self.seat_status.set(f"Loaded seat config from {path.name}")
        except Exception as exc:
            self.seat_status.set(f"Could not load seat config: {exc}")

    def save_seat_config(self) -> None:
        try:
            preferences = self._seat_preferences()
            config_path = self._seat_config_path()
            config_path.write_text(serialize_preferences(preferences), encoding="utf-8")
            self.seat_status.set(f"Saved seat config to {config_path.name}")
        except Exception as exc:
            messagebox.showerror("PureClick Seat", str(exc))
            self.seat_status.set(f"Seat config error: {exc}")

    def copy_seat_config_snippet(self) -> None:
        try:
            preferences = self._seat_preferences()
            self.save_seat_config()
            payload = json.dumps(preferences.to_mapping(), ensure_ascii=False)
            snippet = (
                "// Paste once in DevTools console on tickets.interpark.com\n"
                f'localStorage.setItem("pureclick_seat_v1", {json.dumps(payload)});'
            )
            self.clipboard_clear()
            self.clipboard_append(snippet)
            self.seat_status.set("Copied localStorage config snippet. Paste in browser console once.")
        except Exception as exc:
            messagebox.showerror("PureClick Seat", str(exc))
            self.seat_status.set(f"Seat config snippet error: {exc}")

    def copy_seat_script(self) -> None:
        try:
            self.save_seat_config()
            script_path = app_dir().joinpath("browser", "pureclick_seat_autopilot.user.js")
            if not script_path.exists():
                script_path = app_dir().joinpath("browser", "pureclick_seat_autopilot.js")
            script = script_path.read_text(encoding="utf-8")
            self.clipboard_clear()
            self.clipboard_append(script)
            self.seat_status.set(
                "Copied Tampermonkey userscript. Install on tickets.interpark.com, then use "
                "Copy Config Snippet in the browser console if grades differ from defaults."
            )
        except Exception as exc:
            messagebox.showerror("PureClick Seat", str(exc))
            self.seat_status.set(f"Seat script error: {exc}")


if __name__ == "__main__":
    PureClickApp().mainloop()
