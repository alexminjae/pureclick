from __future__ import annotations

import csv
import ctypes
import json
import platform
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
    enable_windows_dpi_awareness,
    parse_target_time,
    precise_wait_until,
)
from pureclick_watch_core import (
    ConfirmTracker,
    WatchRegion,
    WatchSettings,
    WindowsScreenGrabber,
    classify_frame,
    grid_from_bgra,
    grid_point_to_screen,
    press_refresh_key,
    serialize_settings,
    settings_with_region,
)

IS_WINDOWS = platform.system() == "Windows"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return Path(__file__).resolve().parent


def config_dir() -> Path:
    # Config must be writable and survive app restarts; _MEIPASS is neither.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def virtual_screen_geometry() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the full desktop across all monitors."""
    if IS_WINDOWS:
        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
            user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
            user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
        )
    return 0, 0, 0, 0


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
        self.title("PureClick")
        self.geometry("700x780")
        self.minsize(640, 700)

        self.clock = ServerClock()
        self.clicker = WindowsClicker()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.deadline_perf: float | None = None

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

        self.watch_region: WatchRegion | None = None
        self.watch_tolerance = tk.StringVar(value="40")
        self.watch_min_points = tk.StringVar(value="3")
        self.watch_confirm = tk.StringVar(value="2")
        self.watch_poll_ms = tk.StringVar(value="60")
        self.watch_refresh_s = tk.StringVar(value="0")
        self.watch_region_display = tk.StringVar(value="No watch area selected")
        self.watch_status = tk.StringVar(value="Watch idle")

        self._load_watch_config()
        self._build_ui()
        self._tick_countdown()
        self._tick_server_time()
        self.after(200, self.sync_time)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root = ttk.Frame(self, padding=20)
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

        phase1 = ttk.LabelFrame(root, text="Phase 1 · Timed Click", padding=12)
        phase1.grid(row=3, column=0, sticky="ew", pady=(18, 0))
        phase1.columnconfigure(1, weight=1)

        ttk.Label(phase1, text="Date").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 14))
        ttk.Entry(phase1, textvariable=self.target_date, width=14, justify="center").grid(
            row=0, column=1, sticky="w", pady=6
        )

        ttk.Label(phase1, text="Click at").grid(row=1, column=0, sticky="w", pady=6, padx=(0, 14))
        time_frame = ttk.Frame(phase1)
        time_frame.grid(row=1, column=1, sticky="w", pady=6)
        self._time_number(time_frame, "Hour", self.target_hour, 0, 23, 0, width=4)
        self._time_number(time_frame, "Min", self.target_minute, 0, 59, 2, width=4)
        self._time_number(time_frame, "Sec", self.target_second, 0, 59, 4, width=4)
        self._time_number(time_frame, "Ms", self.target_millisecond, 0, 999, 6, width=5)

        ttk.Label(phase1, text="Location").grid(row=2, column=0, sticky="w", pady=6, padx=(0, 14))
        location_frame = ttk.Frame(phase1)
        location_frame.grid(row=2, column=1, sticky="ew", pady=6)
        location_frame.columnconfigure(0, weight=1)
        ttk.Label(location_frame, textvariable=self.position_display).grid(row=0, column=0, sticky="w")
        self.capture_button = ttk.Button(
            location_frame,
            text="Lock Position",
            command=self.capture_cursor,
        )
        self.capture_button.grid(row=0, column=1, sticky="e", padx=(12, 0))

        button_frame = ttk.Frame(phase1)
        button_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        for column in range(4):
            button_frame.columnconfigure(column, weight=1)
        ttk.Button(button_frame, text="Sync Now", command=self.sync_time).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        ttk.Button(button_frame, text="Test", command=lambda: self.arm(dry_run=True)).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        real_state = "normal" if IS_WINDOWS else "disabled"
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
        status_frame.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(status_frame, text="State").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Label(status_frame, textvariable=self.status).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(status_frame, text="Countdown").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Label(status_frame, textvariable=self.countdown).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(status_frame, text="Sync").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Label(status_frame, textvariable=self.sync_info).grid(row=2, column=1, sticky="w", pady=3)

        watch = ttk.LabelFrame(root, text="Phase 2 · Cancellation Watch", padding=12)
        watch.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        watch.columnconfigure(1, weight=1)

        region_frame = ttk.Frame(watch)
        region_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
        region_frame.columnconfigure(0, weight=1)
        ttk.Label(region_frame, textvariable=self.watch_region_display).grid(row=0, column=0, sticky="w")
        ttk.Button(region_frame, text="Select Watch Area", command=self.select_watch_region).grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )

        tuning = ttk.Frame(watch)
        tuning.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._tuning_field(tuning, "Tolerance", self.watch_tolerance, 0)
        self._tuning_field(tuning, "Min points", self.watch_min_points, 2)
        self._tuning_field(tuning, "Confirm frames", self.watch_confirm, 4)
        tuning2 = ttk.Frame(watch)
        tuning2.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._tuning_field(tuning2, "Poll ms", self.watch_poll_ms, 0)
        self._tuning_field(tuning2, "Auto-refresh s (0=off)", self.watch_refresh_s, 2)

        watch_buttons = ttk.Frame(watch)
        watch_buttons.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for column in range(3):
            watch_buttons.columnconfigure(column, weight=1)
        watch_state = "normal" if IS_WINDOWS else "disabled"
        ttk.Button(
            watch_buttons,
            text="Test Watch",
            command=lambda: self.start_watch(dry_run=True),
            state=watch_state,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(
            watch_buttons,
            text="Start Watch",
            command=lambda: self.start_watch(dry_run=False),
            state=watch_state,
        ).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(watch_buttons, text="Stop", command=self.stop).grid(
            row=0, column=2, sticky="ew", padx=(8, 0)
        )
        ttk.Label(watch, textvariable=self.watch_status, foreground="#555555", wraplength=560).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(
            watch,
            text=(
                "Frame the seat map with Select Watch Area, keep the browser visible and "
                "undisturbed, then Start Watch. When a seat bubble appears in the area, "
                "PureClick clicks it. Auto-refresh presses F5 in the focused window."
            ),
            foreground="#555555",
            wraplength=560,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

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

    def _tuning_field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w", padx=(0, 6))
        ttk.Entry(parent, textvariable=variable, width=7, justify="center").grid(
            row=0, column=column + 1, sticky="w", padx=(0, 16)
        )

    # ------------------------------------------------------- Phase 1 logic

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
        self.position_display.set(f"Click location saved ({x}, {y})")
        self.status.set(f"Location saved at {x}, {y}")
        self.capture_button.configure(state="normal")

    def sync_time(self) -> None:
        self._start_worker(self._sync_worker)

    def arm(self, *, dry_run: bool) -> None:
        self._start_worker(lambda: self._arm_worker(dry_run=dry_run))

    def stop(self) -> None:
        self.stop_event.set()
        self.deadline_perf = None
        self.status.set("Stopped")
        self.watch_status.set("Watch stopped")

    def _start_worker(self, target) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("PureClick", "A task is already running. Press Stop first.")
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
            f"Synced ({result.mode}). Bracket {result.best_rtt_seconds * 1000:.0f} ms, "
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
            mode = "dry run" if dry_run else "real click"
            self._ui(lambda: self.status.set(f"Armed for {mode}"))

            if pre_move_seconds and not dry_run and IS_WINDOWS:
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
                lateness_ms=lateness_ms,
            )
        except Exception as exc:
            message = str(exc)
            self._ui(lambda: self.status.set(f"Error: {message}"))
        finally:
            self.deadline_perf = None

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
        try:
            number = int(text)
        except ValueError as exc:
            raise ValueError(f"{label} must be a number") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"{label} must be between {minimum} and {maximum}")
        return number

    # ------------------------------------------------------- Phase 2 logic

    def select_watch_region(self) -> None:
        left, top, width, height = virtual_screen_geometry()
        if width <= 0 or height <= 0:
            left, top = 0, 0
            width, height = self.winfo_screenwidth(), self.winfo_screenheight()

        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.geometry(f"{width}x{height}+{left}+{top}")
        overlay.attributes("-topmost", True)
        try:
            overlay.attributes("-alpha", 0.25)
        except tk.TclError:
            pass

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.create_text(
            width // 2,
            40,
            text="Drag a box around the seat map · Esc to cancel",
            fill="white",
            font=("TkDefaultFont", 16, "bold"),
        )

        drag: dict[str, int] = {}
        rect_id: list[int] = []

        def on_press(event: tk.Event) -> None:
            drag["x"] = event.x
            drag["y"] = event.y
            rect_id.append(
                canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=3)
            )

        def on_motion(event: tk.Event) -> None:
            if rect_id:
                canvas.coords(rect_id[-1], drag["x"], drag["y"], event.x, event.y)

        def on_release(event: tk.Event) -> None:
            overlay.destroy()
            x0, y0 = min(drag.get("x", 0), event.x), min(drag.get("y", 0), event.y)
            x1, y1 = max(drag.get("x", 0), event.x), max(drag.get("y", 0), event.y)
            try:
                region = WatchRegion(
                    left=left + x0, top=top + y0, width=x1 - x0, height=y1 - y0
                )
            except ValueError as exc:
                self.watch_status.set(f"Selection too small: {exc}")
                return
            self.watch_region = region
            self.watch_region_display.set(
                f"Watching {region.width}x{region.height} at ({region.left}, {region.top})"
            )
            self.watch_status.set("Watch area saved")
            self._save_watch_config()

        overlay.bind("<Escape>", lambda _event: overlay.destroy())
        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.focus_force()

    def _watch_settings(self) -> WatchSettings:
        try:
            settings = WatchSettings(
                tolerance=self._bounded_int(self.watch_tolerance.get(), "Tolerance", 1, 255),
                min_points=self._bounded_int(self.watch_min_points.get(), "Min points", 1, 10_000),
                confirm_frames=self._bounded_int(self.watch_confirm.get(), "Confirm frames", 1, 20),
                poll_ms=self._bounded_int(self.watch_poll_ms.get(), "Poll ms", 10, 5_000),
                refresh_seconds=float(self.watch_refresh_s.get().strip() or "0"),
                region=self.watch_region,
            )
        except ValueError as exc:
            raise PureClickError(str(exc)) from exc
        return settings

    def start_watch(self, *, dry_run: bool) -> None:
        try:
            settings = self._watch_settings()
        except PureClickError as exc:
            messagebox.showerror("PureClick", str(exc))
            return
        if settings.region is None:
            messagebox.showinfo("PureClick", "Select a watch area first.")
            return
        self._save_watch_config()
        self._start_worker(lambda: self._watch_worker(settings, dry_run=dry_run))

    def _watch_worker(self, settings: WatchSettings, *, dry_run: bool) -> None:
        region = settings.region
        assert region is not None
        grabber = None
        try:
            grabber = WindowsScreenGrabber()
            tracker = ConfirmTracker(settings.confirm_frames)
            baseline = grid_from_bgra(
                grabber.grab(region), region.width, region.height, stride=settings.stride
            )
            mode = "dry run" if dry_run else "live"
            self._ui(lambda: self.watch_status.set(f"Watching ({mode})..."))

            poll_seconds = settings.poll_ms / 1000.0
            next_refresh = (
                time.perf_counter() + settings.refresh_seconds
                if settings.refresh_seconds > 0
                else None
            )
            frames = 0
            window_start = time.perf_counter()

            while not self.stop_event.is_set():
                frame_grid = grid_from_bgra(
                    grabber.grab(region), region.width, region.height, stride=settings.stride
                )
                result = classify_frame(
                    baseline,
                    frame_grid,
                    tolerance=settings.tolerance,
                    min_points=settings.min_points,
                    max_fraction=settings.max_fraction,
                )
                frames += 1

                point = tracker.update(result)
                if point is not None:
                    x, y = grid_point_to_screen(region, point, settings.stride)
                    self._beep()
                    if dry_run:
                        self._ui(lambda: self.watch_status.set(
                            f"Change detected at ({x}, {y}) — dry run, no click sent"
                        ))
                    else:
                        self.clicker.click(x, y, hold_ms=self.CLICK_HOLD_MS)
                        self._ui(lambda: self.watch_status.set(f"Clicked change at ({x}, {y})"))
                    self._write_fire_log(mode=f"watch {'dry' if dry_run else 'click'}", x=x, y=y)
                    return

                if result.kind == "unstable":
                    self._ui(lambda: self.watch_status.set(
                        f"Page unstable ({result.changed} points changed) — holding fire"
                    ))
                elif frames % 10 == 0:
                    elapsed = max(time.perf_counter() - window_start, 1e-6)
                    fps = frames / elapsed
                    self._ui(lambda: self.watch_status.set(
                        f"Watching ({mode})... {fps:.0f} fps, last diff {result.changed} points"
                    ))

                if (
                    next_refresh is not None
                    and time.perf_counter() >= next_refresh
                    and result.kind == "quiet"
                ):
                    press_refresh_key()
                    next_refresh = time.perf_counter() + settings.refresh_seconds

                if self.stop_event.wait(poll_seconds):
                    break

            self._ui(lambda: self.watch_status.set("Watch stopped"))
        except Exception as exc:
            message = str(exc)
            self._ui(lambda: self.watch_status.set(f"Watch error: {message}"))
        finally:
            if grabber is not None:
                grabber.close()

    def _beep(self) -> None:
        try:
            import winsound

            winsound.MessageBeep()
        except Exception:
            pass

    # ------------------------------------------------------------ Plumbing

    def _watch_config_path(self) -> Path:
        return config_dir().joinpath("pureclick_watch_config.json")

    def _load_watch_config(self) -> None:
        path = self._watch_config_path()
        if not path.exists():
            return
        try:
            settings = WatchSettings.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            self.watch_tolerance.set(str(settings.tolerance))
            self.watch_min_points.set(str(settings.min_points))
            self.watch_confirm.set(str(settings.confirm_frames))
            self.watch_poll_ms.set(str(settings.poll_ms))
            self.watch_refresh_s.set(str(settings.refresh_seconds))
            if settings.region is not None:
                self.watch_region = settings.region
                self.watch_region_display.set(
                    f"Watching {settings.region.width}x{settings.region.height} "
                    f"at ({settings.region.left}, {settings.region.top})"
                )
        except Exception:
            pass

    def _save_watch_config(self) -> None:
        try:
            settings = self._watch_settings()
            if self.watch_region is not None:
                settings = settings_with_region(settings, self.watch_region)
            self._watch_config_path().write_text(
                serialize_settings(settings), encoding="utf-8"
            )
        except Exception:
            pass

    def _write_fire_log(
        self,
        *,
        mode: str,
        x: int,
        y: int,
        target_unix: float | None = None,
        lateness_ms: float | None = None,
    ) -> None:
        result = self.clock.sync_result
        row = {
            "timestamp": datetime.now(KST).isoformat(timespec="milliseconds"),
            "mode": mode,
            "x": x,
            "y": y,
            "target_server_time": (
                datetime.fromtimestamp(target_unix, KST).isoformat(timespec="milliseconds")
                if target_unix is not None
                else ""
            ),
            "lateness_ms": f"{lateness_ms:.3f}" if lateness_ms is not None else "",
            "offset_ms": f"{(result.offset_seconds * 1000):.3f}" if result else "",
            "bracket_ms": f"{(result.best_rtt_seconds * 1000):.3f}" if result else "",
            "jitter_ms": f"{(result.jitter_seconds * 1000):.3f}" if result else "",
        }
        try:
            path = config_dir().joinpath("pureclick_fire_log.csv")
            exists = path.exists()
            with path.open("a", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=list(row.keys()))
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception:
            pass

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


if __name__ == "__main__":
    enable_windows_dpi_awareness()
    PureClickApp().mainloop()
