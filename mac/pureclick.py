from __future__ import annotations

import csv
import json
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

ROOT_DIR = Path(__file__).resolve().parent.parent
MAC_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(MAC_DIR) not in sys.path:
    sys.path.insert(0, str(MAC_DIR))

from pureclick_core import (  # noqa: E402
    ClickError,
    KST,
    PureClickError,
    ServerClock,
    parse_target_time,
)
from pureclick_mac_core import (  # noqa: E402
    MacClicker,
    MacPrecision,
    ensure_mac_ready,
    precise_wait_until,
)
from pureclick_arm_core import ArmPayload, serialize_arm_payload
from pureclick_seat_core import SeatPreferences
from browser_bridge import BrowserBridge  # noqa: E402

DEFAULT_SEAT = SeatPreferences()


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", MAC_DIR))
    return MAC_DIR


class PureClickMacApp(tk.Tk):
    SYNC_URL = "https://poticket.interpark.com/Book/BookMain.asp"
    SYNC_SAMPLES = 5
    FIRE_OFFSET_MS = 0.0
    PRE_MOVE_MS = 500
    CLICK_HOLD_MS = 0
    RETRY_CLICKS = 2
    RETRY_GAP_MS = 40

    def __init__(self) -> None:
        super().__init__()
        ensure_mac_ready()
        self.title("PureClick")
        self.geometry("420x520")
        self.minsize(380, 480)

        self.clock = ServerClock()
        self.clicker = MacClicker()
        self.browser = BrowserBridge(MAC_DIR)
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.deadline_perf: float | None = None

        today = datetime.now(KST)
        self.target_date = tk.StringVar(value=today.strftime("%Y-%m-%d"))
        self.target_time = tk.StringVar(value=today.strftime("%H:%M:%S"))
        self.x_coord = tk.StringVar(value="")
        self.y_coord = tk.StringVar(value="")
        self.server_time_display = tk.StringVar(value="Syncing...")
        self.show_display = tk.StringVar(value="Open your show in the browser window")
        self.status = tk.StringVar(value="Ready")
        self.countdown = tk.StringVar(value="")

        self._build_ui()
        self._tick_countdown()
        self._tick_server_time()
        self._poll_show_context()
        self._push_seat_config_to_browser()
        self.after(300, self._start_browser)
        self.after(200, self.sync_time)
        self.after(30_000, self._auto_sync)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root = ttk.Frame(self, padding=24)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text="Server time (KST)", font=("TkDefaultFont", 11)).grid(row=0, column=0, sticky="w")
        ttk.Label(root, textvariable=self.server_time_display, font=("TkDefaultFont", 26, "bold")).grid(
            row=1, column=0, sticky="w", pady=(2, 16)
        )

        ttk.Label(root, text="Fire at").grid(row=2, column=0, sticky="w")
        fire = ttk.Frame(root)
        fire.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Entry(fire, textvariable=self.target_date, width=12, justify="center").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(fire, textvariable=self.target_time, width=10, justify="center").grid(row=0, column=1)

        ttk.Label(root, textvariable=self.show_display, foreground="#555555", wraplength=340).grid(
            row=4, column=0, sticky="w", pady=(18, 0)
        )

        self.capture_button = ttk.Button(root, text="1. Lock 예매하기", command=self.capture_cursor)
        self.capture_button.grid(row=5, column=0, sticky="ew", pady=(20, 8))

        self.arm_button = tk.Button(
            root,
            text="2. ARM",
            font=("TkDefaultFont", 18, "bold"),
            height=2,
            command=lambda: self.arm(dry_run=False),
        )
        self.arm_button.grid(row=6, column=0, sticky="ew", pady=(0, 8))

        actions = ttk.Frame(root)
        actions.grid(row=7, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Test timing", command=lambda: self.arm(dry_run=True)).grid(
            row=0, column=0, sticky="ew", padx=(0, 6)
        )
        ttk.Button(actions, text="Stop", command=self.stop).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Label(root, textvariable=self.status, wraplength=340).grid(row=8, column=0, sticky="w", pady=(16, 4))
        ttk.Label(root, textvariable=self.countdown, font=("TkDefaultFont", 13, "bold")).grid(
            row=9, column=0, sticky="w"
        )

        ttk.Label(
            root,
            text="Log in and pick your show in the browser window. Then lock 예매하기 and Arm.",
            foreground="#666666",
            wraplength=340,
        ).grid(row=10, column=0, sticky="w", pady=(20, 0))

    def capture_cursor(self) -> None:
        self.capture_button.configure(state="disabled")
        self._capture_countdown(5)

    def _capture_countdown(self, seconds_left: int) -> None:
        if seconds_left > 0:
            self.status.set(f"Move cursor over 예매하기… {seconds_left}")
            self.after(1000, lambda: self._capture_countdown(seconds_left - 1))
            return
        try:
            x, y = self.clicker.cursor_position()
        except ClickError:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
        self.x_coord.set(str(x))
        self.y_coord.set(str(y))
        self.status.set("예매하기 locked")
        self.capture_button.configure(state="normal")

    def sync_time(self) -> None:
        self._start_worker(self._sync_worker)

    def _auto_sync(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.sync_time()
        self.after(30_000, self._auto_sync)

    def arm(self, *, dry_run: bool) -> None:
        self._start_worker(lambda: self._arm_worker(dry_run=dry_run))

    def stop(self) -> None:
        self.stop_event.set()
        self.deadline_perf = None
        self.status.set("Stopped")

    def _start_worker(self, target) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("PureClick", "Already running.")
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()

    def _sync_worker(self) -> None:
        try:
            self._sync_now()
        except Exception as exc:
            self._ui(lambda: self.status.set(f"Sync failed: {exc}"))

    def _sync_now(self):
        return self.clock.sync_tick(
            self.SYNC_URL,
            sample_count=self.SYNC_SAMPLES,
            min_samples=2,
            max_wait_seconds=8.0,
            poll_seconds=0.005,
        )

    def _poll_show_context(self) -> None:
        context = self.browser.read_page_context()
        if context and context.get("ready"):
            name = context.get("goods_name") or context.get("goods_code")
            self.show_display.set(
                f"Show ready: {name} · {context.get('play_date')} · round {context.get('play_seq')}"
            )
        elif context and context.get("goods_code"):
            self.show_display.set(f"Show {context['goods_code']} — pick date and round in the browser")
        self.after(500, self._poll_show_context)

    def _show_context(self) -> dict:
        context = self.browser.read_page_context()
        if not context or not context.get("ready"):
            raise PureClickError("In the browser, open your show and pick date + round first.")
        return context

    def _arm_payload(self, *, target_unix: float, offset_seconds: float, dry_run: bool) -> ArmPayload:
        context = self._show_context()
        click_x = int(self.x_coord.get()) if self.x_coord.get() else None
        click_y = int(self.y_coord.get()) if self.y_coord.get() else None
        return ArmPayload(
            enabled=True,
            goods_code=str(context["goods_code"]),
            play_date=str(context["play_date"]),
            play_seq=str(context.get("play_seq") or "001"),
            target_server_unix=target_unix,
            offset_seconds=offset_seconds,
            dry_run=dry_run,
            fired=False,
            use_waiting_api=True,
            click_x=click_x,
            click_y=click_y,
        )

    def _on_close(self) -> None:
        self.browser.stop()
        self.destroy()

    def _start_browser(self) -> None:
        try:
            self.browser.start()
        except Exception as exc:
            self.status.set(f"Browser failed: {exc}")

    def _push_seat_config_to_browser(self) -> None:
        try:
            self.browser.push(seat=DEFAULT_SEAT.to_mapping(), reload_autopilot=True)
        except Exception:
            pass

    def _publish_arm_config(self, payload: ArmPayload) -> None:
        path = app_dir().joinpath("pureclick_arm_config.json")
        path.write_text(serialize_arm_payload(payload), encoding="utf-8")
        self._push_seat_config_to_browser()
        self.browser.push(arm=payload.to_mapping(), reload_autopilot=True)

    def _arm_worker(self, *, dry_run: bool) -> None:
        try:
            if not self.x_coord.get() or not self.y_coord.get():
                raise PureClickError("Lock 예매하기 first")
            x = int(self.x_coord.get())
            y = int(self.y_coord.get())
            fire_offset_seconds = self.FIRE_OFFSET_MS / 1000.0
            pre_move_seconds = self.PRE_MOVE_MS / 1000.0

            self._ui(lambda: self.status.set("Syncing…"))
            result = self._sync_now()

            target_unix = parse_target_time(
                self._target_time_text(),
                self.clock.server_time_unix(),
                target_tz=KST,
            )
            deadline_perf = self.clock.deadline_for_server_time(target_unix) + fire_offset_seconds
            if deadline_perf < time.perf_counter() - 0.100:
                raise PureClickError("That time is already in the past")

            self.deadline_perf = deadline_perf
            payload = self._arm_payload(
                target_unix=target_unix,
                offset_seconds=result.offset_seconds,
                dry_run=dry_run,
            )
            self._publish_arm_config(payload)
            label = "Test armed" if dry_run else "Armed"
            self._ui(lambda: self.status.set(label))

            if pre_move_seconds and not dry_run:
                move_deadline = max(time.perf_counter(), deadline_perf - pre_move_seconds)
                if precise_wait_until(move_deadline, stop_event=self.stop_event):
                    self.clicker.move_to(x, y)

            final_wait_start = max(time.perf_counter(), deadline_perf - 2.0)
            if precise_wait_until(final_wait_start, stop_event=self.stop_event):
                with MacPrecision():
                    should_fire = precise_wait_until(deadline_perf, stop_event=self.stop_event)
            else:
                should_fire = False

            if not should_fire:
                self._ui(lambda: self.status.set("Stopped"))
                return

            fired_perf = time.perf_counter()
            lateness_ms = (fired_perf - deadline_perf) * 1000
            if dry_run:
                self._ui(lambda: self.status.set(f"Test OK ({lateness_ms:+.1f} ms)"))
            else:
                self.clicker.click(x, y, hold_ms=self.CLICK_HOLD_MS)
                for _ in range(self.RETRY_CLICKS):
                    if self.stop_event.wait(self.RETRY_GAP_MS / 1000.0):
                        break
                    self.clicker.click(x, y, hold_ms=self.CLICK_HOLD_MS)
                self._ui(lambda: self.status.set(f"Fired ({lateness_ms:+.1f} ms)"))
            self._write_fire_log(
                mode="dry run" if dry_run else "real click",
                x=x,
                y=y,
                target_unix=target_unix,
                deadline_perf=deadline_perf,
                fired_perf=fired_perf,
                lateness_ms=lateness_ms,
            )
        except Exception as exc:
            self._ui(lambda: self.status.set(str(exc)))
        finally:
            self.deadline_perf = None

    def _target_time_text(self) -> str:
        date_text = self.target_date.get().strip()
        time_text = self.target_time.get().strip()
        datetime.strptime(date_text, "%Y-%m-%d")
        if len(time_text.split(":")) == 2:
            time_text += ":00"
        hour, minute, second = time_text.split(":")
        for part, label in ((hour, "Hour"), (minute, "Minute"), (second, "Second")):
            if not part.isdigit():
                raise PureClickError(f"{label} must be a number")
        return f"{date_text} {int(hour):02d}:{int(minute):02d}:{int(second):02d}.000"

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
        path = app_dir().joinpath("pureclick_fire_log.csv")
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _tick_countdown(self) -> None:
        if self.deadline_perf is None:
            self.countdown.set("")
        else:
            remaining = self.deadline_perf - time.perf_counter()
            self.countdown.set(f"{max(0.0, remaining):.1f}s")
        self.after(100, self._tick_countdown)

    def _tick_server_time(self) -> None:
        result = self.clock.sync_result
        if result is None:
            self.server_time_display.set("Syncing…")
        else:
            server_time = datetime.fromtimestamp(self.clock.server_time_unix(), KST)
            millis = server_time.microsecond // 1000
            self.server_time_display.set(server_time.strftime("%H:%M:%S") + f".{millis:03d}")
        self.after(100, self._tick_server_time)

    def _ui(self, callback) -> None:
        self.after(0, callback)


if __name__ == "__main__":
    PureClickMacApp().mainloop()
