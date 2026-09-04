from __future__ import annotations

import json
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MAC_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(MAC_DIR) not in sys.path:
    sys.path.insert(0, str(MAC_DIR))

from browser_bridge import BrowserBridge  # noqa: E402
from core.arm import ArmPayload, clamp_entry_offset_ms, default_entry_offset_ms  # noqa: E402
from core.entry import needs_parking, park_url  # noqa: E402
from core.mode import (  # noqa: E402
    BEFORE_OPEN, MODE_LABELS, OPEN, derive_mode, guidance as mode_guidance, sale_phase,
)
from core.clock import KST, NolSniperError, ServerClock, parse_target_time  # noqa: E402
import app_platform  # noqa: E402
import app_update  # noqa: E402
from core.seat import (  # noqa: E402
    SeatPreferences,
    parse_goods_code,
    bridge_line,
    bridge_status,
    live_state,
    waiting_log_lines,
    click_log_lines,
    serialize_preferences,
)
from core.showinfo import seat_table_lines, fetch_round_remains, fetch_show_catalog, fetch_goods_info_rounds  # noqa: E402
from core.watch_trigger import TriggerState, next_trigger_state  # noqa: E402
from core.zone_map import (  # noqa: E402
    block_keys_in_watch_rect,
    live_block_keys,
    is_click,
    parse_box,
    parse_watch_rect,
    project_venue,
    seat_pitch,
    seats_in_watch_rect,
)

# MAC_DIR stays the real script directory — sys.path depends on it. Saved
# preferences need somewhere that survives a frozen build's launch, where
# MAC_DIR resolves inside sys._MEIPASS and is wiped every time the app starts;
# a source checkout keeps writing next to the script, as before.
DATA_DIR = app_platform.user_data_dir() if getattr(sys, "frozen", False) else MAC_DIR

# How long an auto-park may take before the arm goes out anyway. A park that has
# not landed by then is still worth arming behind — the page may arrive during
# the countdown — but blocking the arm indefinitely on a slow navigation would
# lose the open outright.
PARK_SETTLE_SECONDS = 6.0

# One instrument, not a cockpit.
#
# The old palette ran five competing hues — green ground, green accent, blue
# hover, bright-green primary, amber warnings — and wrapped every group in a
# hairline box. Colour then carried no meaning and the two buttons that matter
# were lost among the boxes.
#
# Now: ink ground, a single hot accent that means "this is the action / this is
# live", and one success colour that appears exactly once, when a seat is taken.
# Everything else is graded warm grey. Grouping comes from space and type
# weight, not from borders.
BG = "#0B0B0D"        # page
# A card has to actually look like a card. PANEL was #131316 against a #0B0B0D
# page — eight levels apart out of 255, which is invisible on any real display,
# so the sections ran together into one column no matter how they were padded.
# Lifting the surface and giving the hairline genuine contrast is what separates
# them; the spacing was never the problem.
PANEL = "#1A1A21"     # raised surface: a section card
PANEL_2 = "#24242C"   # inputs, and the live band
BORDER = "#34343E"    # visible hairline around every card
FG = "#F2F0EE"        # warm white
MUTED = "#8A8A93"
FAINT = "#55555E"
ACCENT = "#FF4D2E"    # the action, and the live pulse
GREEN = "#3DDC97"     # only ever "seat taken"
AMBER = "#E8B84B"
ACCENT_2 = ACCENT
# A seat outside the drawn range: present, but not being watched.
SEAT_IDLE = "#5A6B62"

# `core.seat.live_state` decides what the live band says and must not know this
# palette, so the tone names it returns are turned into colour here and nowhere
# else.
TONES = {"green": GREEN, "accent": ACCENT, "amber": AMBER, "faint": FAINT}

# The Korean face, and a monospace for the clock and countdown.
#
# Tk does not raise on an unknown family — it silently falls back to its own
# default. Naming a macOS-only face therefore collapsed the entire typographic
# system on Windows, including the 34pt countdown, whose digits then jitter
# sideways on every tick because the fallback is proportional.
if sys.platform == "win32":
    UI_FONT = "Malgun Gothic"
    MONO_FONT = "Consolas"
else:
    # Apple SD Gothic Neo renders 한글 far better than any Latin-first default;
    # SF Mono is not exposed to Tk, and Menlo is the same lineage and is.
    UI_FONT = "Apple SD Gothic Neo"
    MONO_FONT = "Menlo"
DANGER = "#ef4444"


class NolSniperApp(tk.Tk):
    # Aiming strategies, in the order they appear in the picker.
    # All three go for the stage first; the label's tail says which side of the
    # house to look at when several seats are equally close.
    NO_SKETCH_NOTICE = ("아직 좌석 배치도가 저장되지 않은 공연입니다. "
                        "[지금 진입] 후 실제 공연장 좌석도가 동기화됩니다.")
    STRATEGY_LABELS = {
        "center": "무대 가까운 순 · 가운데 (기본)",
        "left": "무대 가까운 순 · 왼쪽",
        "right": "무대 가까운 순 · 오른쪽",
    }
    # Mirrors CATCH_MIN_POLL_MS in the autopilot; used only to say how long a
    # sweep takes, never to drive the poll.

    # A config written by an older build must still open.
    LEGACY_STRATEGIES = {"stage": "center", "random": "center"}

    # Offsets from now, in seconds. Long enough to watch the countdown, short
    # enough that a rehearsal is not itself a wait.
    # Sync against the host we fire at, not a third one.
    #
    # This pointed at poticket.interpark.com while the queue lives on
    # api-ticketfront. Measured by boundary-bracketing their Date headers:
    # poticket runs +18ms, ticketfront -8ms, tickets.interpark.com +4ms — so the
    # two hosts that matter for booking agree within 12ms and poticket is the
    # outlier, 26ms away from the one the entry actually races against.
    # The host root: a Date header, no body, and nothing show-specific to go
    # stale. /v1/goods/{code}/waiting works too but would tie the clock to one
    # show outliving the sync.
    SYNC_URL = "https://api-ticketfront.interpark.com/"
    SYNC_SAMPLES = 5
    START_URL = "https://nol.yanolja.com/ticket"

    def __init__(self) -> None:
        # Before super(), deliberately: this decides how Tk reads the display,
        # and once the root exists the process DPI mode can no longer change.
        app_platform.prepare_display()
        super().__init__()
        # Was ensure_mac_ready(), which raised on any non-Darwin system — the
        # first thing that stopped this app existing on Windows. app_platform
        # checks whatever this platform actually needs, and on Windows that
        # means naming the WebView2 runtime with a download link rather than
        # letting pywebview fail later with nothing actionable.
        app_platform.ensure_ready()
        # On screen, not just in a manifest check nobody sees: the Windows
        # build went through six point releases in one troubleshooting
        # session, sent back and forth as identically-named zip files, and
        # there was no way to look at the running app and tell which one it
        # actually was — "still blank" and "still on the old build" look
        # identical from the outside. version_tag() below is exactly this.
        self.title(f"NOL 스나이퍼 · 조작판 {app_update.version_tag()}")
        # The two windows are one app, so they are tiled rather than stacked.
        # Both used to open centred at their natural size and the 예매 창, being
        # the wider of the two, covered all but a narrow strip of this one.
        self.panel_geometry, self.browser_geometry = self._plan_layout()
        px, py, pw, ph = self.panel_geometry
        self.minsize(460, 700)
        self.geometry(f"{pw}x{ph}+{px}+{py}")
        self.configure(bg=BG)

        self.clock = ServerClock()
        self.browser = BrowserBridge(MAC_DIR)
        self.worker: threading.Thread | None = None
        self._catalog: dict | None = None
        self._show_info_data: dict | None = None
        self._grade_rows: list[dict] = []
        self._block_rows: list[dict] = []
        self._auto_loaded_code: str | None = None
        # The code a lookup is currently in flight for. Separate from
        # _auto_loaded_code, which means "loaded successfully": the panel polls
        # the page every 500ms, so without this a retry would re-fire the fetch
        # on every tick while the first one is still running.
        self._fetching_code: str | None = None
        self._followed_round: tuple | None = None
        self._zones: tk.Toplevel | None = None
        self._zone_canvas: tk.Canvas | None = None
        self._zone_hint: tk.StringVar | None = None
        self._zone_view = None
        self._zone_sketch: list[dict] = []
        self._zone_drag: tuple[float, float] | None = None
        self._zone_redraw_job: str | None = None
        self._block_selecting = False

        now = datetime.now(KST)
        self.target_date = tk.StringVar(value=now.strftime("%Y-%m-%d"))
        self.target_time = tk.StringVar(value=now.strftime("%H:%M:%S"))
        # A separate moment for rehearsals, so testing never disturbs the real
        # 티켓 오픈 you have set. Defaults to a minute out — far enough to watch
        # the countdown, close enough to not be a wait.
        soon = now + timedelta(minutes=1)
        self.test_date = tk.StringVar(value=soon.strftime("%Y-%m-%d"))
        self.test_result = tk.StringVar(value="")
        # The moment the scheduler is actually aimed at, and whether it is a
        # rehearsal. None means nothing is armed and the countdown falls back to
        # 티켓 오픈.
        self._armed_target_unix: float | None = None
        self._armed_is_test = False
        self._arming_test = False
        self.test_hour = tk.StringVar(value=soon.strftime("%H"))
        self.test_minute = tk.StringVar(value=soon.strftime("%M"))
        self.test_second = tk.StringVar(value=soon.strftime("%S"))
        self.show_round = tk.StringVar(value="")
        self.open_note = tk.StringVar(value="아직 안 열린 공연 — 열리는 순간 대기열을 먼저 잡습니다.")
        self.catch_note = tk.StringVar(value="이미 매진된 공연 — 고른 범위에서 자리가 나오면 바로 잡습니다.")
        self.server_time = tk.StringVar(value="동기화 중…")
        self.show_title = tk.StringVar(value="공연을 선택하세요")
        self.show_where = tk.StringVar(value="예매 창에서 공연을 열면 자동으로 채워집니다")
        self.countdown = tk.StringVar(value="")
        # The user's own ms correction on the fire moment. Negative fires early.
        #
        # NOL's backend does not flip at exactly 티켓 오픈 and the page needs a
        # beat after that, so the moment worth aiming at is not the published
        # one — and it differs per show. This is the number you tune between
        # rehearsals; it applies to 대기 시작 and 테스트 실행 alike, so what you
        # measured in a rehearsal is what runs on the day.
        self.entry_offset_ms = tk.StringVar(value="0")
        self.fire_preview = tk.StringVar(value="")
        self.zone_summary = tk.StringVar(value="감시 구역: 전체")
        self.clock_info = tk.StringVar(value="서버 시각 동기화 중…")
        self.bridge = tk.StringVar(value="예매 창 연결 대기 중…")
        # Folded into the bridge line rather than given a widget of its own.
        # Every "computed but invisible" bug in this app started as a variable
        # bound to nothing, and this one only ever has a sentence to say.
        self._update_note = ""
        self.guidance = tk.StringVar(
            value="지금 할 일 — 다른 창(NOL 예매)에서 공연을 클릭하세요. 조작판이 자동으로 채워집니다."
        )
        self.btn_arm = None
        self.btn_catch = None
        self.status = tk.StringVar(value="준비")
        self.reason = tk.StringVar(value="")
        self.mode_banner = tk.StringVar(value="")
        self.mode_text = tk.StringVar(value="")
        self.action_note = tk.StringVar(value="")
        self._mode = "no_show"
        self._last_arm_status: dict = {}
        self._band_candidate: tuple | None = None
        self._band_drawn: tuple | None = None
        # A button press, and what the 예매 창 looked like when it was made, so
        # a command nothing was there to receive can be told from one that was.
        self._asked: tuple[str, float, tuple] | None = None
        # Until when the band is holding a message about something the user just
        # did. Without it those live for exactly one 500ms poll — see _flash.
        self._flash_until = 0.0
        # The dot's colour, kept because `_load_seat_config` can set a state
        # before any widget exists — a config file that will not parse reported
        # itself in words while the dot stayed grey, saying nothing was wrong.
        self._state_colour = FAINT
        # Which part of the map to aim for, so this instance is not racing every
        # other macro for the same front-row seat.
        self.seat_strategy = tk.StringVar(value="center")
        self.block_keys = tk.StringVar(value="")
        # Dragged region on the copied grape map, in seatMeta coords.
        self._watch_rect: dict[str, float] | None = None

        # Off by default. This was `instant`, which defaulted on and had no
        # control anywhere, so the autopilot fired the moment the 예매 창 reached
        # a seat map whether or not that was wanted.
        self.auto_start_on = tk.BooleanVar(value=False)
        self.reentry_on = tk.BooleanVar(value=True)
        self.auto_assign_on = tk.BooleanVar(value=False)
        # The re-sync runs on its own thread, never the shared worker slot —
        # see _resync_now(). This is only the "one at a time" latch.
        self._resyncing = False
        self._resync_thread: threading.Thread | None = None
        # 진입 점검: set when the press goes out, cleared when its report is
        # drawn, so a stale entry result cannot overwrite it a tick later.
        self._entry_probe_pending = False
        self._entry_probe_at: str | None = None
        # The watch's pace is the autopilot's to decide: it holds a request
        # budget (requests per second) that keeps the gateway quiet, and a
        # number typed here can only make it slower. This was 400 and floored
        # at 400 on load, which silently overrode the 200ms budget — the sweep
        # stayed at its old speed no matter what the autopilot intended.
        self.speed_ms = tk.StringVar(value="0")
        self.quantity = tk.StringVar(value="1")
        self.birth = tk.StringVar(value="")
        self.delivery = tk.StringVar(value="배송")
        self.payment = tk.StringVar(value="무통장입금")
        self.block_names = tk.StringVar(value="")
        self.discord = tk.StringVar(value="")
        self.product_url = tk.StringVar(value="")
        # The whole-venue trigger the panel keeps for the page (see
        # _start_trigger_worker); nothing runs until 감시 시작.
        self._trigger_state: TriggerState | None = None
        self._trigger_on = False
        self._trigger_thread: threading.Thread | None = None
        self.goods_code = tk.StringVar(value="")
        self.place_code = tk.StringVar(value="")
        self.play_date = tk.StringVar(value="")
        self.play_seq = tk.StringVar(value="001")
        # The 일정 picker. `rounds` is goods-info's playSeqList as the page
        # published it; `round_choice` is the label currently shown. The pair
        # exists because a combobox can only hold strings, and the thing the
        # entry actually needs is the playSeq behind the label.
        self.rounds: list[dict[str, str]] = []
        self.round_choice = tk.StringVar(value="")
        self.round_note = tk.StringVar(value="공연을 열면 날짜·회차가 여기에 나옵니다.")
        # 고급 knobs are off the main surface by default: none of them is needed
        # to enter a show, and every one of them was a question the panel could
        # not answer for someone who just wants a ticket.
        self.advanced_open = tk.BooleanVar(value=False)
        self.play_time = tk.StringVar(value="")
        self._remain_refresh_key: tuple | None = None
        self._remain_refreshing = False

        self._load_seat_config()
        # Push straight away. The seat config on disk is what the 예매 창 reads,
        # so a value corrected on load has to be republished or the browser goes
        # on using the stale one until a control happens to be touched.
        self.after(120, self._push_seat_config)
        self._style()
        self._build_ui()
        self.after(300, self._start_browser)
        self.after(400, self._poll_show)
        self.after(100, self._tick_server_time)
        self.after(250, self._sync_now_bg)
        # Off the main thread and after the UI exists: a manifest fetch must
        # never be between the user and a window.
        self.after(1200, self._check_updates_bg)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _plan_layout(self) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Split the screen: control panel on the left, 예매 창 on the right.

        Returns (x, y, width, height) for each. The seat map needs the wider
        half — it is the thing being read during a race — so the panel takes a
        third and never less than a readable 460.
        """
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        # 28 is the macOS menu bar and 40 the Dock. Windows has neither, so
        # those constants left a gap at the top and put the window bottom under
        # the taskbar — whose height Tk cannot report, hence the more
        # conservative reserve there.
        if sys.platform == "win32":
            top = 0
            bottom_reserve = 56
        else:
            top = 28
            bottom_reserve = 40
        height = max(640, screen_h - top - bottom_reserve)

        panel_w = max(460, min(600, int(screen_w * 0.32)))
        browser_w = max(760, screen_w - panel_w)
        # A screen too narrow for both keeps the panel readable and lets the
        # 예매 창 overlap rather than shrinking it into uselessness.
        if panel_w + browser_w > screen_w:
            browser_w = max(760, screen_w - panel_w)

        return (0, top, panel_w, height), (panel_w, top, browser_w, height)

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        body = (UI_FONT, 12)
        # A Combobox popup is a plain Tk Listbox, not a ttk widget, so it ignores
        # every style set below and renders in system colours — black on white
        # against this palette. Only `option_add` reaches it.
        for option, value in (
            ("*TCombobox*Listbox.background", PANEL_2),
            ("*TCombobox*Listbox.foreground", FG),
            ("*TCombobox*Listbox.selectBackground", ACCENT),
            ("*TCombobox*Listbox.selectForeground", BG),
            ("*TCombobox*Listbox.font", body),
        ):
            self.option_add(option, value)
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL_2, font=body)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=body)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(UI_FONT, 11))
        style.configure("Faint.TLabel", background=BG, foreground=FAINT, font=(UI_FONT, 11))

        # Section headings: small, dim, wide-set. They label a region of space
        # rather than draw a box around it — and they are grey, not accent.
        # Colouring every heading made each one shout as loudly as the button
        # that actually does something, which is most of what made this screen
        # feel like an instrument panel.
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(UI_FONT, 15, "bold"))
        style.configure("Wordmark.TLabel", background=BG, foreground=FG, font=(UI_FONT, 15, "bold"))
        style.configure("Clock.TLabel", background=BG, foreground=FG, font=(MONO_FONT, 15))

        style.configure("TCheckbutton", background=BG, foreground=FG, font=body)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=FG, borderwidth=0,
                        insertcolor=FG, padding=6)
        style.configure("TCombobox", fieldbackground=PANEL_2, foreground=FG, borderwidth=0,
                        arrowcolor=MUTED, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_2)],
                  foreground=[("readonly", FG)])

        # One filled button style, used only for the two things worth doing.
        style.configure("Primary.TButton", background=ACCENT, foreground="#120603",
                        font=(UI_FONT, 13, "bold"), padding=(14, 13), borderwidth=0)
        style.map("Primary.TButton",
                  background=[("active", "#FF6A4E"), ("disabled", "#2A211F")],
                  foreground=[("disabled", FAINT)])
        # Everything else is a ghost: legible, never competing.
        style.configure("Ghost.TButton", background=BG, foreground=MUTED,
                        font=(UI_FONT, 12), padding=(12, 10), borderwidth=0)
        style.map("Ghost.TButton",
                  background=[("active", PANEL_2)],
                  foreground=[("active", FG), ("disabled", FAINT)])
        style.configure("TButton", background=PANEL_2, foreground=FG,
                        font=(UI_FONT, 12), padding=(12, 10), borderwidth=0)
        style.map("TButton", background=[("active", BORDER), ("disabled", "#17171B")],
                  foreground=[("disabled", FAINT)])

        # Card variants. A ttk widget carries its own background, so anything
        # placed on a raised surface has to be told about it or it paints the
        # page colour and punches a hole in the card.
        style.configure("Card.TFrame", background=PANEL)
        style.configure("CardTitle.TLabel", background=PANEL, foreground=FG, font=(UI_FONT, 15, "bold"))
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED, font=(UI_FONT, 11))
        style.configure("CardFaint.TLabel", background=PANEL, foreground=FAINT, font=(UI_FONT, 11))
        style.configure("CardHero.TLabel", background=PANEL, foreground=FG, font=(MONO_FONT, 34))
        style.configure("CardGhost.TButton", background=PANEL, foreground=MUTED,
                        font=(UI_FONT, 12), padding=(12, 10), borderwidth=0)
        style.map("CardGhost.TButton",
                  background=[("active", PANEL_2)],
                  foreground=[("active", FG), ("disabled", FAINT)])

        style.configure("TSeparator", background=BORDER)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        rowheight=26, borderwidth=0, font=(UI_FONT, 11))
        style.configure("Treeview.Heading", background=BG, foreground=MUTED, borderwidth=0)
        style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#120603")])



    def _scrollable_root(self) -> ttk.Frame:
        """A vertically scrolling body.

        Stacked in a third-width window the sections are taller than the screen,
        and anything that did not fit was simply unreachable — which is how the
        status area ended up permanently off the bottom.
        """
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, bg=BG, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        body = ttk.Frame(canvas, padding=16)
        holder = canvas.create_window((0, 0), window=body, anchor="nw")
        # Hold the top edge still while the column changes height. Tk keeps the
        # *fraction* scrolled when the scrollregion changes, so every label that
        # grew or shrank by a line shifted everything under the pointer — the
        # "scroll jumps while I read" the panel was known for. Re-anchor to the
        # pixel that was at the top before the change.
        def on_body_resize(_e: tk.Event) -> None:
            top = canvas.canvasy(0)
            canvas.configure(scrollregion=canvas.bbox("all"))
            box = canvas.bbox("all")
            height = max(1, (box[3] - box[1]) if box else 1)
            canvas.yview_moveto(max(0.0, min(1.0, top / height)))
        body.bind("<Configure>", on_body_resize)
        self._scroll_canvas = canvas
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(holder, width=e.width))

        # Scoped to the pointer, so the 오픈 예정 목록 window keeps its own wheel.
        #
        # Tk reports one notch as ±1 on macOS and ±120 on Windows, so the same
        # arithmetic scrolls 120 units per notch there. This panel is taller
        # than the screen by design — that is why it scrolls at all — so on
        # Windows it was one notch from unusable.
        notch = 120 if sys.platform == "win32" else 1

        def wheel(event: tk.Event) -> None:
            steps = int(event.delta / notch) or (1 if event.delta > 0 else -1)
            canvas.yview_scroll(-steps, "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return body

    def _card(self, parent, label: str, note: object = None) -> ttk.Frame:
        """A distinct surface for one function.

        The two functions used to run together down a single column, separated
        only by spacing, and each could be folded away behind a click. They are
        the whole point of the app: they get their own ground and they stay
        open.
        """
        # Border by inset: tkinter has no border-colour on a Frame, so the
        # hairline is an outer frame the inner surface sits 1px inside.
        outer = tk.Frame(parent, bg=BORDER)
        outer.pack(fill="x", pady=(14, 0))
        shell = tk.Frame(outer, bg=PANEL)
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(shell, bg=PANEL)
        head.pack(fill="x", padx=18, pady=(16, 0))
        tk.Label(head, text=label, bg=PANEL, fg=FG, anchor="w",
                 font=(UI_FONT, 14, "bold")).pack(anchor="w")
        if note is not None:
            tk.Label(head, textvariable=note, bg=PANEL, fg=MUTED, anchor="nw", height=2,
                     wraplength=self.panel_geometry[2] - 110, justify="left",
                     font=(UI_FONT, 11)).pack(fill="x", pady=(3, 0))
        body = tk.Frame(shell, bg=PANEL)
        body.pack(fill="x", padx=18, pady=(12, 18))
        return body

    def _build_ui(self) -> None:
        """Two functions, both always visible, and a clock to run them against.

        Headings are dim and small; the accent belongs to the buttons and the
        live dot and nothing else. Colour used for emphasis everywhere is what
        made this read as an instrument panel rather than a tool.
        """
        wrap = self.panel_geometry[2] - 76
        # Pinned before anything else is packed. tk's packer hands the bottom
        # strip to the first claimant and the remainder to the later
        # expand=True widget, so building the band first is what keeps it out
        # of the scroll region.
        self._build_live_band(wrap)
        root = self._scrollable_root()

        # --- Masthead ---------------------------------------------------------
        head = ttk.Frame(root)
        head.pack(fill="x")
        ttk.Label(head, text="스나이퍼", style="Wordmark.TLabel").pack(side="left")
        ttk.Label(head, textvariable=self.server_time, style="Clock.TLabel").pack(side="right")
        ttk.Label(root, textvariable=self.clock_info, style="Faint.TLabel").pack(anchor="e", pady=(1, 0))
        # Directly under the masthead, because it says whether anything below it
        # can be trusted. The panel had no way to tell a live browser from one it
        # had stopped hearing from, and every failure looks the same from here.
        # Every live label in the column reserves its worst case (height=N
        # lines), so text that changes on the 500ms poll never resizes the
        # column. See on_body_resize for why that mattered.
        tk.Label(root, textvariable=self.bridge, bg=BG, fg=MUTED, anchor="nw", height=2,
                 font=(UI_FONT, 11), wraplength=wrap, justify="left").pack(fill="x", pady=(6, 0))

        # --- What we are aiming at -------------------------------------------
        show = self._card(root, "공연")
        tk.Label(show, textvariable=self.show_title, bg=PANEL, fg=FG, anchor="nw", height=2,
                 font=(UI_FONT, 15, "bold"), wraplength=wrap - 40, justify="left").pack(fill="x")
        tk.Label(show, textvariable=self.show_where, bg=PANEL, fg=MUTED, anchor="nw", height=1,
                 font=(UI_FONT, 11), wraplength=wrap - 40, justify="left").pack(fill="x", pady=(3, 0))
        # The 회차 belongs on screen: it changes underneath everything else and
        # every seat the macro targets is keyed to it.
        tk.Label(show, textvariable=self.show_round, bg=PANEL, fg=FAINT, anchor="nw", height=1,
                 font=(UI_FONT, 11), wraplength=wrap - 40, justify="left").pack(fill="x", pady=(2, 0))
        self.seat_table = tk.Text(show, height=3, bg=PANEL, fg=MUTED, insertbackground=FG,
                                  highlightthickness=0, borderwidth=0, wrap="none",
                                  font=(MONO_FONT, 11), spacing1=2)
        self.seat_table.pack(fill="x", pady=(10, 0))
        self.seat_table.configure(state="disabled")
        url_row = ttk.Frame(show, style="Card.TFrame")
        url_row.pack(fill="x", pady=(10, 0))
        url_row.columnconfigure(0, weight=1)
        ttk.Entry(url_row, textvariable=self.product_url).grid(row=0, column=0, sticky="ew")
        ttk.Button(url_row, text="불러오기", style="CardGhost.TButton",
                   command=self.fetch_show).grid(row=0, column=1, padx=(8, 0))

        # --- What to do next --------------------------------------------------
        self.tip_edge = tk.Frame(root, bg=BORDER)
        self.tip_edge.pack(fill="x", pady=(16, 0))
        tip = tk.Frame(self.tip_edge, bg=PANEL_2)
        tip.pack(fill="both", expand=True, padx=1, pady=1)
        # The mode, as one word, and the one thing to do about it. Both come
        # from core.mode and nothing else, so they cannot contradict each other
        # or the live band below.
        tk.Label(tip, textvariable=self.mode_banner, bg=PANEL_2, fg=ACCENT, anchor="w",
                 font=(UI_FONT, 12, "bold"), height=1).pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(tip, textvariable=self.guidance, bg=PANEL_2, fg=FG, anchor="nw", height=3,
                 font=(UI_FONT, 12), wraplength=wrap - 24,
                 justify="left").pack(fill="x", padx=14, pady=(2, 12))

        # --- How it chooses ---------------------------------------------------
        # Kept because the tip box above is packed and unpacked as the macro
        # starts and stops, and `before=` is how it returns to its own place
        # rather than to the bottom of the column.
        # 좌석 고르는 순서 and 매수 now live under 고급 설정 (built below); the
        # main surface is show → round → checkbox → one action.
        self.aim_card = None
        # The per-attempt ranking used to be drawn here — stage distances, the
        # top five candidates, and what each kind of map move cost. It changed
        # on every poll and resized the card with it, which shoved the whole
        # column up and down twice a second. It is a debugging read, not a
        # racing one, and it lives in the published status instead.

        # --- Open 대기 ---------------------------------------------------------
        openq = self._card(root, "오픈 대기", self.open_note)

        # --- 1. Which round -----------------------------------------------------
        # First, because it is the only thing the user has to decide, and because
        # entering without it is what put people on the 일정 선택 page mid-flow.
        tk.Label(openq, text="① 날짜·회차", bg=PANEL, fg=FG,
                 font=(UI_FONT, 12, "bold"), anchor="w").pack(anchor="w")
        self.round_box = ttk.Combobox(openq, textvariable=self.round_choice,
                                      state="readonly", values=[])
        self.round_box.pack(fill="x", pady=(6, 0))
        self.round_box.bind("<<ComboboxSelected>>", self._on_round_pick)
        tk.Label(openq, textvariable=self.round_note, bg=PANEL, fg=FAINT, anchor="nw", height=2,
                 justify="left", wraplength=wrap - 40,
                 font=(UI_FONT, 11)).pack(fill="x", pady=(4, 0))

        # --- 2. The one action --------------------------------------------------
        # (티켓 오픈 시각 is filled from the picked round and lives under 고급.)
        tk.Frame(openq, bg=BORDER, height=1).pack(fill="x", pady=(14, 12))
        tk.Label(openq, text="② 실행", bg=PANEL, fg=FG,
                 font=(UI_FONT, 12, "bold"), anchor="w").pack(anchor="w", pady=(0, 8))

        # Always packed, blank when there is nothing to count: packing and
        # unpacking it moved every card below by a hero line.
        self.countdown_label = tk.Label(openq, textvariable=self.countdown, bg=PANEL, fg=FG,
                                        anchor="w", height=1, font=(MONO_FONT, 34))
        self.countdown_label.pack(fill="x", pady=(8, 0))
        self.btn_arm = ttk.Button(openq, text="오픈에 자동 진입", style="Primary.TButton", command=self.arm)
        self.btn_arm.pack(fill="x", pady=(8, 0))
        self.btn_arm_stop = ttk.Button(openq, text="대기 중지", style="CardGhost.TButton",
                                       command=self.stop_arm)
        self.btn_arm_stop.pack(fill="x", pady=(4, 0))
        # A show that is already open has nothing to count down to, and 대기 시작
        # against a past 티켓 오픈 answered 이미 지난 시각입니다 — leaving the one
        # case where entry is instant with no button that does anything.
        self.btn_enter_now = ttk.Button(openq, text="지금 진입", style="CardGhost.TButton",
                                        command=self.enter_now)
        self.btn_enter_now.pack(fill="x", pady=(4, 0))
        # Which of the two is off right now, and why. Fixed height: it changes
        # with the mode and must not move the checkbox under the pointer.
        tk.Label(openq, textvariable=self.action_note, bg=PANEL, fg=FAINT, anchor="nw", height=2,
                 justify="left", wraplength=wrap - 40,
                 font=(UI_FONT, 11)).pack(fill="x", pady=(2, 0))
        tk.Checkbutton(openq, text="들어가면 좌석까지 잡기", variable=self.auto_start_on,
                       command=self._push_seat_config, bg=PANEL, fg=FG, selectcolor=PANEL_2,
                       activebackground=PANEL, activeforeground=FG, highlightthickness=0,
                       font=(UI_FONT, 12), anchor="w").pack(anchor="w", pady=(12, 0))
        tk.Label(openq, text="보안문자만 직접 입력하면 됩니다.", bg=PANEL, fg=FAINT,
                 font=(UI_FONT, 11), anchor="w").pack(anchor="w", pady=(2, 0))
        # The virtual seat map, on the entry card too — it targets 지금 진입's
        # grab, not only 취켓팅. Drawing a box here makes the autopilot strike
        # inside it the instant the seat map opens; with no box it takes the
        # seat nearest the stage centre. Reachable before the show opens as long
        # as the venue has been seen once (its layout is cached).
        vrow = tk.Frame(openq, bg=PANEL)
        vrow.pack(fill="x", pady=(12, 0))
        vrow.columnconfigure(0, weight=1)
        tk.Label(vrow, textvariable=self.zone_summary, bg=PANEL, fg=FG, anchor="nw", height=2,
                 wraplength=wrap - 150, justify="left", font=(UI_FONT, 11)).grid(row=0, column=0, sticky="w")
        ttk.Button(vrow, text="가상 좌석판", style="CardGhost.TButton",
                   command=self.open_zone_picker).grid(row=0, column=1)
        tk.Label(openq, text="가상 좌석판에서 목표 구역을 드래그하면 그 안에서만 잡습니다. 비워 두면 무대 중앙 최우선.",
                 bg=PANEL, fg=FAINT, anchor="nw", height=2, justify="left", wraplength=wrap - 40,
                 font=(UI_FONT, 11)).pack(fill="x", pady=(2, 0))

        # --- 고급 ---------------------------------------------------------------
        # Everything below is a rehearsal or a tuning knob. None of it is needed
        # to get a ticket, and having it on the main surface made the panel read
        # as five competing actions instead of one.
        tk.Frame(openq, bg=BORDER, height=1).pack(fill="x", pady=(16, 10))
        self.btn_advanced = ttk.Button(openq, text="고급 설정 ▸", style="CardGhost.TButton",
                                       command=self._toggle_advanced)
        self.btn_advanced.pack(fill="x")
        self.advanced_box = tk.Frame(openq, bg=PANEL)
        self.aim_card = self.advanced_box
        # 티켓 오픈 시각 — normally filled from the picked round; editable here
        # for a show whose open time the API does not know.
        when = tk.Frame(self.advanced_box, bg=PANEL)
        when.pack(fill="x", pady=(12, 0))
        tk.Label(when, text="티켓 오픈", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 11)).pack(side="left", padx=(0, 10))
        ttk.Entry(when, textvariable=self.target_date, width=11).pack(side="left")
        ttk.Entry(when, textvariable=self.target_time, width=9).pack(side="left", padx=(6, 0))
        tk.Label(self.advanced_box, text="회차를 고르면 자동으로 채워집니다.", bg=PANEL, fg=FAINT,
                 anchor="w", font=(UI_FONT, 11)).pack(anchor="w", pady=(2, 0))
        # 좌석 고르는 순서 and 매수.
        pick = tk.Frame(self.advanced_box, bg=PANEL)
        pick.pack(fill="x", pady=(12, 0))
        pick.columnconfigure(0, weight=1)
        self.strategy_box = ttk.Combobox(pick, values=list(self.STRATEGY_LABELS.values()),
                                         state="readonly")
        self.strategy_box.grid(row=0, column=0, sticky="ew")
        self.strategy_box.set(
            self.STRATEGY_LABELS.get(self.seat_strategy.get(), self.STRATEGY_LABELS["center"])
        )
        self.strategy_box.bind("<<ComboboxSelected>>", self._on_strategy_pick)
        tk.Label(pick, text="매수", bg=PANEL, fg=FAINT, font=(UI_FONT, 11)).grid(row=0, column=1, padx=(12, 6))
        ttk.Combobox(pick, textvariable=self.quantity, values=["1", "2", "3", "4"],
                     state="readonly", width=3).grid(row=0, column=2)

        # The ms correction. Off the main surface: entry is a single API call
        # the server timestamps itself, so there is nothing here for a normal
        # open to correct.
        offset_row = tk.Frame(self.advanced_box, bg=PANEL)
        offset_row.pack(fill="x", pady=(12, 0))
        tk.Label(offset_row, text="진입 보정", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 11)).pack(side="left", padx=(0, 8))
        ttk.Entry(offset_row, textvariable=self.entry_offset_ms, width=7).pack(side="left")
        tk.Label(offset_row, text="ms · 음수면 더 일찍", bg=PANEL, fg=FAINT,
                 font=(UI_FONT, 11)).pack(side="left", padx=(6, 0))
        tk.Label(self.advanced_box, text="오픈 시각보다 이만큼 일찍/늦게 요청합니다. 보통은 0으로 둡니다.",
                 bg=PANEL, fg=FAINT, anchor="w", justify="left", wraplength=wrap - 40,
                 font=(UI_FONT, 11)).pack(anchor="w", pady=(2, 0))
        tk.Label(self.advanced_box, textvariable=self.fire_preview, bg=PANEL, fg=FAINT,
                 anchor="w", font=(MONO_FONT, 11)).pack(anchor="w", pady=(2, 0))
        for var in (self.entry_offset_ms, self.target_date, self.target_time):
            var.trace_add("write", lambda *_: self._refresh_fire_preview())
        self._refresh_fire_preview()

        openq = self.advanced_box

        # --- Rehearsal ---------------------------------------------------------
        # The open is one instant that either works or is lost, and it could only
        # ever be found out in real time. This runs the same entry against a
        # moment you choose.
        tk.Frame(openq, bg=BORDER, height=1).pack(fill="x", pady=(16, 14))
        # Pick the moment rather than type it. Typing a date and a time to
        # rehearse something a minute away is friction with no purpose, and a
        # mistyped one fails with "이미 지난 시각입니다" instead of testing.
        test_row = tk.Frame(openq, bg=PANEL)
        test_row.pack(fill="x")
        tk.Label(test_row, text="테스트 시각", bg=PANEL, fg=MUTED,
                 font=(UI_FONT, 11)).pack(side="left", padx=(0, 8))
        ttk.Entry(test_row, textvariable=self.test_date, width=11).pack(side="left")
        for var, count, pad in ((self.test_hour, 24, (8, 0)),
                                (self.test_minute, 60, (4, 0)),
                                (self.test_second, 60, (4, 0))):
            ttk.Combobox(test_row, textvariable=var, state="readonly", width=3,
                         values=[f"{n:02d}" for n in range(count)]).pack(side="left", padx=pad)
        ttk.Button(test_row, text="+1분", style="CardGhost.TButton", width=5,
                   command=self._bump_test_time).pack(side="left", padx=(10, 0))

        run_row = tk.Frame(openq, bg=PANEL)
        run_row.pack(fill="x", pady=(10, 0))
        run_row.columnconfigure(0, weight=1)
        self.btn_test = ttk.Button(run_row, text="테스트 실행", style="CardGhost.TButton",
                                   command=self.run_entry_test)
        self.btn_test.grid(row=0, column=0, sticky="ew")
        ttk.Button(run_row, text="오픈 시각으로", style="CardGhost.TButton",
                   command=self._test_time_from_show).grid(row=0, column=1, padx=(6, 0))
        tk.Label(openq, text="정한 시각에 실제로 이 공연에 들어가 봅니다.", bg=PANEL, fg=FAINT,
                 font=(UI_FONT, 11), anchor="w").pack(anchor="w", pady=(2, 0))

        # The safe half of the rehearsal, and the reason the device clock never
        # needs touching. 테스트 실행 enters for real, so on a show that has not
        # opened the only way to see anything was to shift the system clock
        # forward — which froze this panel's own clock, stranded 취켓팅's
        # cooldowns, and proved nothing about the button anyway, because a
        # forward-shifted clock does not open somebody else's backend.
        ttk.Button(openq, text="진입 점검", style="CardGhost.TButton",
                   command=self.run_entry_probe).pack(fill="x", pady=(10, 0))
        tk.Label(openq, text="아무것도 누르지 않고, 지금 무엇으로 어떻게 들어갈지만 확인합니다.",
                 bg=PANEL, fg=FAINT, font=(UI_FONT, 11), anchor="w").pack(anchor="w", pady=(2, 0))
        tk.Label(openq, textvariable=self.test_result, bg=PANEL, fg=GREEN, anchor="nw", height=8,
                 justify="left", font=(MONO_FONT, 11)).pack(fill="x", pady=(8, 0))

        # --- 취켓팅 -------------------------------------------------------------
        catch = self._card(root, "취켓팅", self.catch_note)
        zone = tk.Frame(catch, bg=PANEL)
        zone.pack(fill="x")
        zone.columnconfigure(0, weight=1)
        tk.Label(zone, textvariable=self.zone_summary, bg=PANEL, fg=FG, anchor="nw", height=2,
                 wraplength=wrap - 130, justify="left",
                 font=(UI_FONT, 12)).grid(row=0, column=0, sticky="w")
        ttk.Button(zone, text="범위 정하기", style="CardGhost.TButton",
                   command=self.open_zone_picker).grid(row=0, column=1)
        self.btn_catch = ttk.Button(catch, text="감시 시작", style="Primary.TButton",
                                    command=self.start_catch)
        self.btn_catch.pack(fill="x", pady=(12, 0))
        ttk.Button(catch, text="감시 중지", style="CardGhost.TButton", command=self.stop_all).pack(
            fill="x", pady=(4, 0)
        )

        self._update_guidance(None)

    def _build_live_band(self, wrap: int) -> None:
        """What is happening, pinned where it cannot be scrolled away.

        This is the thing you watch while a race runs, and it spent this whole
        design as the last widget *inside* the scrolling body — which on a
        third-width window with the cards open put it below the fold exactly
        when it mattered. `_scrollable_root` was added because "the status area
        ended up permanently off the bottom"; it made the column reachable, not
        the status visible. Being outside the scroll is the actual fix.
        """
        holder = tk.Frame(self, bg=BG)
        holder.pack(side="bottom", fill="x", padx=16, pady=(0, 16))

        live_edge = tk.Frame(holder, bg=BORDER)
        live_edge.pack(fill="x")
        live = tk.Frame(live_edge, bg=PANEL_2)
        live.pack(fill="both", expand=True, padx=1, pady=1)
        dot_row = tk.Frame(live, bg=PANEL_2)
        dot_row.pack(fill="x", padx=16, pady=(14, 0))
        self.status_dot = tk.Label(dot_row, text="●", bg=PANEL_2, fg=self._state_colour,
                                   font=(UI_FONT, 11))
        self.status_dot.pack(side="left")
        # The mode word, beside the dot: the same enum the tip box shows.
        tk.Label(dot_row, textvariable=self.mode_text, bg=PANEL_2, fg=MUTED, anchor="w",
                 height=1, font=(UI_FONT, 11, "bold")).pack(side="left", padx=(8, 0))
        # Every line reserves its worst case, so the band never changes size.
        # It used to: the headline and the reason both changed on the same
        # 500ms tick, and a reason that grew from one wrapped line to two moved
        # the whole band. A little reserved whitespace is the price of an
        # instrument that holds still while you read it.
        tk.Label(live, textvariable=self.status, bg=PANEL_2, fg=FG, anchor="w",
                 font=(UI_FONT, 13, "bold"), wraplength=wrap - 20, height=2,
                 justify="left").pack(fill="x", padx=16)
        tk.Label(live, textvariable=self.reason, bg=PANEL_2, fg=MUTED, anchor="w",
                 font=(UI_FONT, 11), wraplength=wrap - 20, height=3,
                 justify="left").pack(fill="x", padx=16, pady=(2, 14))
        # No number line. It held 시도/구역/빈자리/스윕, every one of which moves
        # on the 500ms repaint, and a panel whose numbers churn continuously
        # cannot be read while a race is on. They are still published in the
        # status for debugging; they are not something to watch.

        ttk.Button(holder, text="전부 정지", style="Ghost.TButton",
                   command=self.stop_all).pack(fill="x", pady=(10, 0))
        # Outside the scroll, beside 전부 정지, because it is needed at exactly
        # the moment nothing else is working. The 조작판 does not die with the
        # 예매 창 and never should have needed a full relaunch to recover.
        ttk.Button(holder, text="예매 창 다시 열기", style="Ghost.TButton",
                   command=self.reopen_browser).pack(fill="x", pady=(6, 0))



    def stop_all(self) -> None:
        try:
            self._stop_trigger_worker()
            self._remember_press("전부 정지")
            self.browser.send_command("stop_all", clear_arm=True)
            self._flash(FAINT, "정지 요청됨", "예매 창에 정지를 전달했습니다.")
        except Exception as exc:
            self._flash(AMBER, "정지하지 못했습니다", str(exc))

    def _seat_preferences(self) -> SeatPreferences:
        # Empty grade order is valid: it means "take the best seat in any grade".
        blocks = [part.strip() for part in self.block_names.get().split(",") if part.strip()]
        return SeatPreferences.from_mapping(
            {
                # Grade is not a filter: any seat inside the chosen area or
                # strategy will do. These stay in the config because the JS and
                # the tests still read them; they are simply never narrowed.
                "grade_order": [],
                "grade_strict": False,
                "allow_group_seats": True,
                "auto_assign": self.auto_assign_on.get(),
                "block_keys": self.block_keys.get(),
                "block_names": blocks,
                "watch_rect": self._watch_rect,
                "poll_ms": self.speed_ms.get().strip(),
                "speed_ms": self.speed_ms.get().strip(),
                "quantity": self.quantity.get().strip(),
                "birth_yymmdd": self.birth.get().strip(),
                "delivery": self.delivery.get(),
                "payment": self.payment.get(),
                "discord_webhook": self.discord.get().strip(),
                "seat_strategy": self.seat_strategy.get(),
                "catch_grade_strict": False,
                "reentry": self.reentry_on.get(),
                "adjacent": True,
                "auto_seats_after_entry": self.auto_start_on.get(),
                # Carried here only so it survives a restart; the fire reads it
                # off the arm payload.
                "entry_offset_ms": self._entry_offset_ms(),
            }
        )

    def _push_seat_config(
        self,
        *,
        reload_autopilot: bool = False,
        command: str | None = None,
        clear_arm: bool = False,
    ) -> None:
        preferences = self._seat_preferences()
        config_path = DATA_DIR / "nolsniper_seat_config.json"
        config_path.write_text(serialize_preferences(preferences), encoding="utf-8")
        self.browser.push(
            seat=preferences.to_mapping(),
            reload_autopilot=reload_autopilot,
            command=command,
            clear_arm=clear_arm,
        )

    def _load_seat_config(self) -> None:
        path = DATA_DIR / "nolsniper_seat_config.json"
        if not path.exists():
            return
        try:
            preferences = SeatPreferences.from_mapping(json.loads(path.read_text(encoding="utf-8")))
            self.block_names.set(",".join(preferences.block_names or preferences.block_keys))
            self.block_keys.set(",".join(preferences.block_keys))
            if preferences.watch_rect:
                left, top, right, bottom = preferences.watch_rect
                self._watch_rect = {"left": left, "top": top, "right": right, "bottom": bottom}
            else:
                self._watch_rect = None
            # Never carry a saved throttle forward: the watch paces itself,
            # and a stale 400 from an older config silently halves its speed.
            self.speed_ms.set("0")
            self.quantity.set(str(preferences.quantity))
            self.birth.set(preferences.birth_yymmdd)
            self.delivery.set(preferences.delivery)
            self.payment.set(preferences.payment)
            self.discord.set(preferences.discord_webhook)
            strategy = self.LEGACY_STRATEGIES.get(
                preferences.seat_strategy, preferences.seat_strategy
            )
            # 가운데 is the default: an empty or unrecognised saved strategy
            # loads as 무대 가까운 순 · 가운데 rather than a stale side choice.
            if strategy not in self.STRATEGY_LABELS:
                strategy = "center"
            self.seat_strategy.set(strategy)
            self.auto_assign_on.set(preferences.auto_assign)
            self.reentry_on.set(preferences.reentry)
            self.entry_offset_ms.set(str(preferences.entry_offset_ms))
            self.auto_start_on.set(preferences.auto_seats_after_entry)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop startup
            # Silently falling back to defaults meant settings you had chosen
            # simply disappeared between runs, with nothing to explain it.
            self._note(f"저장된 설정을 읽지 못해 기본값으로 시작합니다: {exc}", error=True)

    def _sync_now_bg(self) -> None:
        self._start_worker(self._sync_worker)

    def _check_updates_bg(self) -> None:
        """Ask once, on a worker, whether there is a newer build or automation.

        A downloaded exe is a snapshot, but the automation inside it does not
        have to be: it tracks someone else's markup and changes far more often
        than the Python does. Nothing here installs anything — a newer exe is
        reported with a link, and a newer autopilot is only cached after its
        SHA-256 matched the manifest. Every failure ends up in this note rather
        than being swallowed, because an unverified script must never look like
        a verified one.
        """
        if not app_update.manifest_url():
            return
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        try:
            status = app_update.check(
                app_update.manifest_url(),
                current_version=app_update.app_version(),
                cache_path=self._autopilot_cache_path(),
            )
            note = status.note
        except Exception as exc:  # noqa: BLE001 - a check must never take the app down
            note = f"업데이트 확인 중 오류: {str(exc)[:60]}"
        self._ui(self._set_update_note, note)

    @staticmethod
    def _autopilot_cache_path():
        import browser_host

        return browser_host.autopilot_cache_path()

    def _set_update_note(self, note: str) -> None:
        self._update_note = note.strip()

    def _resync_now(self) -> None:
        """Re-measure the server clock, on a thread of its own.

        Deliberately not `_start_worker`. That shares one slot with 대기 시작 and
        테스트 실행 and *returns False* when something else is running — and the
        caller set `_resyncing = True` before asking. So a re-sync that arrived
        while any other work was in flight never started, its `finally` never
        ran, and the latch stayed True for the rest of the session: the panel
        silently stopped re-syncing forever.

        That is the "서버 시각이 멈췄다" report. Shifting the device clock
        forward and back is exactly the thing that triggers a re-sync, and doing
        it while the panel was busy was enough to freeze the clock permanently.
        A re-sync competes with nothing, so it gets its own thread and cannot be
        refused.
        """
        if self._resyncing:
            return
        self._resyncing = True
        self._resync_thread = threading.Thread(
            target=self._resync_worker, name="nolsniper-resync", daemon=True
        )
        self._resync_thread.start()

    def _resync_worker(self) -> None:
        """Re-measure after a sleep or a clock change; the next tick retries."""
        try:
            result = self._sync_now()
        except Exception as exc:  # noqa: BLE001 - a failed re-sync must not be silent
            self._ui(self._note, f"서버 시각을 다시 맞추지 못했습니다: {exc}", error=True)
        else:
            self._ui(
                self._note,
                f"서버 시각을 다시 맞췄습니다 (보정 {result.offset_seconds * 1000:+.0f}ms)",
            )
        finally:
            self._resyncing = False


    ENGINE_LABELS = {
        "onestop-reserved": "지정석",
        "onestop-sports": "스포츠",
        "onestop-general-admission": "비지정석",
        "legacy-poticket": "구형(미지원)",
    }





    def fetch_show(self, *, navigate: bool = True) -> None:
        """Look up the show over HTTP.

        The public ticketfront API answers without a login and covers every
        booking engine, so it is the primary source. `navigate` is off when the
        show came from the browser itself, otherwise the panel would push the
        browser back to a page it is already on.
        """
        target = self.product_url.get().strip() or self.goods_code.get().strip()
        if not target:
            self.status.set("예매 창에서 공연을 열거나, 상품코드를 입력하세요")
            return
        self.status.set("공연 정보 조회 중…")
        threading.Thread(target=self._fetch_show_worker, args=(target,), daemon=True).start()

        if not navigate:
            return
        # A manually typed code should also move the browser, otherwise the panel
        # and the window disagree about which show is being worked on.
        try:
            code = parse_goods_code(target)
            self.goods_code.set(code)
            # In flight, not loaded — the worker started two lines above. Marking
            # it loaded here would suppress the retry if that lookup fails.
            self._fetching_code = code
            self.browser.navigate(f"https://nol.yanolja.com/ticket/products/{code}")
            self._note(f"브라우저를 {code} 공연 페이지로 이동합니다")
        except Exception as exc:
            self._note(f"페이지 이동 생략: {exc}", error=True)

    def _fetch_show_worker(self, target: str) -> None:
        try:
            catalog = fetch_show_catalog(target)
        except Exception as exc:  # noqa: BLE001 - any failure is reported in the UI
            self._ui(self.status.set, f"조회 실패: {exc}")
            self._ui(self._note, f"공연 정보를 못 가져왔습니다: {exc}", error=True)
            return
        finally:
            # Whichever way it ended, the next poll may try again.
            self._fetching_code = None
        self._ui(self._apply_show_info, catalog.to_mapping())

    def _apply_show_info(self, info: dict) -> None:
        self._show_info_data = info
        if info.get("goods_code"):
            code = str(info["goods_code"])
            self.goods_code.set(code)
            self._auto_loaded_code = code
        if info.get("place_code"):
            self.place_code.set(str(info["place_code"]))
        # Hand goods + place to the page now, not at arm time: the 일정 picker
        # fills from goods-info, which needs the place code the page often
        # cannot read for itself (측정: 엘리자벳 blank, 드라큘라 L-code).
        try:
            self.browser.publish_show(str(info.get("goods_code") or self.goods_code.get()),
                                      str(info.get("place_code") or self.place_code.get()))
        except Exception:  # noqa: BLE001 - a hint, never a blocker
            pass
        # The API only knows the run's first date; if the browser is showing a
        # particular date, that is the one being booked.
        # Deliberately NOT seeding play_date from play_start_date: that is the
        # run's first night, not a round, and it leaked into the arm as the date
        # to enter. The picker sets play_date from the chosen round instead.
        play_seqs = info.get("play_seqs") or []
        if play_seqs and self.play_seq.get() not in play_seqs:
            self.play_seq.set(str(play_seqs[0]))

        hide_remain = bool(info.get("hide_remain_seat"))
        self._grade_rows = list(info.get("grades") or [])

        if info.get("ticket_open_kst"):
            self._set_open_time(str(info["ticket_open_kst"]))

        name = info.get("goods_name") or info.get("goods_code") or "공연"
        place = info.get("place_name") or ""
        opens = info.get("ticket_open_kst") or "미정"
        self.show_title.set(name)
        self._refresh_show_where(place=place)
        self._render_seat_table(self._grade_rows, hide_remain)

        # Collected and then dropped into an unrendered variable — these say
        # things like "등급 정보를 가져오지 못했습니다", which is exactly what a
        # confused user needs to see.
        warnings = list(info.get("warnings") or [])
        errors = list(info.get("errors") or [])
        # Never overwrite a live arm's band: 오픈 대기 중 must stay clean. The
        # lookup result goes to the status line only when nothing is running.
        if not getattr(self, "_armed_target_unix", None):
            self.status.set(f"공연 불러옴 · 등급 {len(self._grade_rows)}개")
        if errors:
            # A real failure to read the show — the only thing worth a red box.
            self._note(" · ".join(errors), error=True)
        elif warnings:
            # Informational (입장권, 스포츠…): the reason line, never the red box.
            self._note(" · ".join(warnings))
        else:
            self._note(f"상품 {info.get('goods_code')} · 회차 {self.play_seq.get()}")
        # The picker must never sit blank: if the page has not delivered rounds
        # yet, ask goods-info directly with the place code we now hold.
        if not (getattr(self, "rounds", None) or []):
            goods = str(info.get("goods_code") or self.goods_code.get() or "")
            place = str(info.get("place_code") or self.place_code.get() or "")
            self._start_worker(lambda: self._fetch_rounds_fallback(goods, place))
        self._update_guidance(self.browser.read_page_context())
        # Initial lookup uses play_start_date (often 0석). If the 예매판 already
        # has a date/round selected, replace those zeros immediately.
        context = self.browser.read_page_context() or {}
        if context.get("play_date"):
            self._apply_context_fields(context)
            self._schedule_remain_refresh(context)

    def _render_seat_table(
        self,
        rows: list[dict],
        hide_remain: bool,
        live_free: int | None = None,
        free_by_grade: dict | None = None,
    ) -> None:
        """Grades, prices and what is left.

        The text itself is built by `seat_table_lines` so it can be tested
        without a display; this method only puts it on screen.
        """
        text = "\n".join(seat_table_lines(rows, hide_remain, live_free, free_by_grade))
        self.seat_table.configure(state="normal")
        self.seat_table.delete("1.0", tk.END)
        self.seat_table.insert("1.0", text)
        self.seat_table.configure(state="disabled")

    def _pretty_play_date(self, raw: str | None = None) -> str:
        date = re.sub(r"\D", "", str(raw or self.play_date.get() or ""))
        if len(date) < 8:
            return ""
        return f"{date[0:4]}.{date[4:6]}.{date[6:8]}"

    def _refresh_show_where(self, *, place: str | None = None) -> None:
        info = self._show_info_data or {}
        place_text = place if place is not None else str(info.get("place_name") or "")
        pretty = self._pretty_play_date()
        time_text = self._clock_text(self.play_time.get())
        seq = self.play_seq.get().strip()
        parts = [part for part in (pretty, time_text) if part]
        if seq:
            parts.append(f"회차 {seq}")
        self.show_where.set(" · ".join(part for part in (" · ".join(parts), place_text) if part) or place_text)

    def _grade_remain_signature(self, rows: list[dict] | None = None) -> tuple:
        source = rows if rows is not None else self._grade_rows
        return tuple(
            (str(row.get("name") or ""), int(row.get("remain") or 0), str(row.get("grade") or ""))
            for row in source
        )

    def _merge_live_grades(self, grades: list[dict]) -> bool:
        """Update the seat table from live 예매판 remains. Returns True if changed."""
        if not grades:
            return False
        price_by_name = {
            str(row.get("name") or ""): int(row.get("price") or 0) for row in self._grade_rows
        }
        price_by_grade = {
            str(row.get("grade") or ""): int(row.get("price") or 0) for row in self._grade_rows
        }
        merged: list[dict] = []
        for row in grades:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("seatGradeName") or "").strip()
            grade = str(row.get("grade") or row.get("seatGrade") or "").strip()
            if not name and not grade:
                continue
            price = int(
                row.get("price")
                or price_by_name.get(name)
                or price_by_grade.get(grade)
                or 0
            )
            merged.append(
                {
                    "grade": grade or name,
                    "name": name or grade,
                    "price": price,
                    "remain": int(row.get("remain") or row.get("remainCnt") or 0),
                }
            )
        if not merged:
            return False
        if self._grade_remain_signature(merged) == self._grade_remain_signature():
            return False

        self._grade_rows = merged
        hide_remain = bool((self._show_info_data or {}).get("hide_remain_seat"))
        self._render_seat_table(self._grade_rows, hide_remain)
        self._refresh_show_where()
        return True

    def _schedule_remain_refresh(self, context: dict | None = None) -> None:
        ctx = context or {}
        play_date = str(ctx.get("play_date") or self.play_date.get() or "").replace("-", "")
        play_seq = str(ctx.get("play_seq") or self.play_seq.get() or "")
        play_time = str(ctx.get("play_time") or self.play_time.get() or "")
        goods = self.goods_code.get().strip()
        if not goods or len(re.sub(r"\D", "", play_date)) != 8:
            return
        key = (goods, play_date, play_seq, play_time)
        if key == self._remain_refresh_key or self._remain_refreshing:
            return
        self._remain_refresh_key = key
        place = str(ctx.get("place_code") or self.place_code.get() or "")
        threading.Thread(
            target=self._remain_refresh_worker,
            args=(goods, place, play_date, play_seq, play_time),
            daemon=True,
        ).start()

    def _remain_refresh_worker(
        self,
        goods: str,
        place: str,
        play_date: str,
        play_seq: str,
        play_time: str,
    ) -> None:
        self._remain_refreshing = True
        try:
            prices = {
                str(row.get("grade") or ""): {
                    "price": int(row.get("price") or 0),
                    "name": str(row.get("name") or ""),
                }
                for row in self._grade_rows
                if row.get("grade")
            }
            grades, resolved_seq, resolved_time = fetch_round_remains(
                goods,
                play_date,
                place_code=place,
                play_seq=play_seq or None,
                play_time=play_time or None,
                prices=prices,
            )
            payload = {
                "grades": [grade.to_mapping() for grade in grades],
                "play_seq": resolved_seq,
                "play_time": resolved_time,
                "play_date": play_date,
            }
            self._ui(self._apply_round_remains, payload)
        except Exception as exc:  # noqa: BLE001
            self._ui(self._note, f"잔여석 동기화 실패: {exc}", error=True)
        finally:
            self._remain_refreshing = False

    def _apply_round_remains(self, payload: dict) -> None:
        if payload.get("play_date"):
            self.play_date.set(str(payload["play_date"]))
        if payload.get("play_seq"):
            self.play_seq.set(str(payload["play_seq"]))
        if payload.get("play_time"):
            self.play_time.set(str(payload["play_time"]))
        if self._merge_live_grades(list(payload.get("grades") or [])):
            round_label = " ".join(
                part
                for part in (self._pretty_play_date(), self.play_time.get().strip())
                if part
            )
            self._note(
                f"예매판 회차 동기화 · {round_label or self.play_seq.get()} · "
                f"잔여 {sum(int(r.get('remain') or 0) for r in self._grade_rows)}석"
            )

    def stop_arm(self) -> None:
        """Cancel a pending queue entry.

        Absence of the arm key is what disarms the scheduler — browser_host
        removes the localStorage entry when the state file has no `arm`.
        """
        try:
            self.browser.push(clear_arm=True)
            # Nothing is aimed at anything any more, so the countdown goes back
            # to 티켓 오픈 rather than counting down to a moment that will not
            # fire.
            self._armed_target_unix = None
            self._armed_is_test = False
            self._flash(FAINT, "대기 취소됨", "오픈 대기를 해제했습니다.")
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            self._flash(AMBER, "대기를 해제하지 못했습니다", str(exc))

    def _zone_window_size(self) -> str:
        """Shape the window to the venue so the map is big enough to aim in.

        A fixed 760x660 fits the venue inside it on one axis and pads the other.
        Tall halls suffer worst: 26011315 is 397 wide by 793 tall, so it fit on
        height at scale 0.509 and left half the width empty, with seats drawn
        sub-pixel and a drag too coarse to place. Matching the window to the
        venue's proportions spends the whole canvas on seats.
        """
        chrome_w, chrome_h = 28, 160  # padding + hint + button row
        min_w, min_h, max_w, max_h = 520, 460, 1200, 900
        if not self._zone_sketch:
            return "760x660"
        xs = [row.get("x") for row in self._zone_sketch if row.get("x") is not None]
        ys = [row.get("y") for row in self._zone_sketch if row.get("y") is not None]
        if not xs or not ys:
            return "760x660"
        span_x = (max(xs) - min(xs)) or 1.0
        span_y = (max(ys) - min(ys)) or 1.0

        # Fit the venue into the largest allowed canvas, then keep only the box
        # it actually fills — the leftover was the wasted margin.
        scale = min((max_w - chrome_w) / span_x, (max_h - chrome_h) / span_y)
        width = int(min(max_w, max(min_w, span_x * scale + chrome_w)))
        height = int(min(max_h, max(min_h, span_y * scale + chrome_h)))
        return f"{width}x{height}"

    def open_zone_picker(self) -> None:
        """Drag a box over the venue; the watch covers whatever is inside it.

        Deliberately the only control here. 취켓팅 takes whatever frees up in the
        area, at any grade, nearest the stage first — so there is nothing to
        configure beyond where to look.
        """
        if self._zones is not None and tk.Toplevel.winfo_exists(self._zones):
            self._zones.lift()
            return

        # Fill the map before the window is drawn: on a goods/product page there
        # is no live seat map, so load the venue layout cached for this show —
        # and if none was ever cached, fall back to a default block layout so
        # the user can always drag a target instead of seeing a black screen.
        if not self._zone_sketch:
            code = ""
            try:
                code = self.goods_code.get().strip() or self._resolved_goods()
            except Exception:  # noqa: BLE001 - no code yet is fine; default map covers it
                code = ""
            if code:
                self._load_cached_sketch(code)
            # No cached layout: the canvas shows the notice below rather than a
            # generic venue — seats that are not this show's read as a bug.

        window = tk.Toplevel(self)
        window.title("가상 좌석판 — 목표 구역")
        window.geometry(self._zone_window_size())
        window.configure(bg=BG)
        self._zones = window

        ttk.Label(
            window,
            text="드래그해서 목표 구역을 정하세요. [지금 진입]의 좌석 잡기와 취켓팅 모두 그 안에서만, 등급과 무관하게 잡습니다. 비워 두면 무대 중앙에 가장 가까운 자리부터.",
            style="Muted.TLabel",
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(14, 2))

        self._zone_hint = tk.StringVar(value=self._zone_hint_text())
        ttk.Label(window, textvariable=self._zone_hint, style="Muted.TLabel").pack(
            anchor="w", padx=14, pady=(0, 8)
        )

        self._zone_canvas = tk.Canvas(window, bg=PANEL, highlightthickness=0)
        self._zone_canvas.pack(fill="both", expand=True, padx=14)
        self._zone_canvas.bind("<Button-1>", self._zone_drag_start)
        self._zone_canvas.bind("<B1-Motion>", self._zone_drag_move)
        self._zone_canvas.bind("<ButtonRelease-1>", self._zone_drag_end)
        self._zone_canvas.bind("<Configure>", lambda _e: self._schedule_zone_map())

        buttons = ttk.Frame(window)
        buttons.pack(fill="x", padx=14, pady=14)
        ttk.Button(buttons, text="전체 해제", command=self._clear_watch_rect).pack(side="left")
        ttk.Button(buttons, text="닫기", command=self._close_zone_picker).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", self._close_zone_picker)

        self._schedule_zone_map()

    def _close_zone_picker(self) -> None:
        window, self._zones = self._zones, None
        self._zone_canvas = None
        self._zone_hint = None
        self._zone_view = None
        if window is not None:
            window.destroy()

    def _zone_hint_text(self) -> str:
        """How many seats are actually in play, and how many are only scenery.

        A range is a boundary, not a snapshot: the seats inside it that are
        taken right now are the whole point of 취켓팅. So this counts seats in
        blocks that are on sale this round, and names — separately — the blocks
        that are not being sold at all, where nothing can ever come free.
        """
        if not self._zone_sketch:
            return self.NO_SKETCH_NOTICE
        # Counted from what is on screen, not from the sketch: they differ by
        # the far side blocks house_frame keeps out of the drawing, and a hint
        # naming more dark seats than the map shows reads as a miscount.
        live_keys = live_block_keys(self._zone_sketch)
        live_count = sum(1 for row in self._zone_sketch if row.get("k") in live_keys)
        hidden = len({row.get("k") for row in self._zone_sketch}) - len(live_keys)
        # Named, not drawn. Without this a venue whose map is narrower than the
        # 예매 창's looks like a picker that has lost part of the room.
        tail = f" · 미판매 {hidden}구역은 지도에 없습니다" if hidden else ""
        if self._watch_rect is None:
            return f"{live_count}석{tail} · 범위를 지정하지 않으면 전체를 감시합니다."
        inside = [
            row for row in seats_in_watch_rect(self._zone_sketch, self._rect_tuple())
            if row.get("k") in live_block_keys(self._zone_sketch)
        ]
        return f"{live_count}석 중 {len(inside)}석 감시 중{tail}"

    def _rect_tuple(self) -> tuple[float, float, float, float] | None:
        rect = self._watch_rect
        if not rect:
            return None
        return (rect["left"], rect["top"], rect["right"], rect["bottom"])

    def _clear_zone_canvas(self) -> None:
        """Wipe the virtual seat map at once.

        Called the moment the show changes, before anything for the new show
        is loaded: the redraw is scheduled, not immediate, and until it ran the
        previous venue stayed on screen under the new show's name.
        """
        self._zone_view = None
        canvas = getattr(self, "_zone_canvas", None)
        if canvas is not None and canvas.winfo_exists():
            canvas.delete("all")
        if getattr(self, "_zone_hint", None) is not None:
            self._zone_hint.set("새 공연의 좌석 배치를 불러오는 중…")

    def _redraw_zone_map(self) -> None:
        """Paint the venue and the current watch box."""
        self._zone_redraw_job = None
        canvas = self._zone_canvas
        if canvas is None or not canvas.winfo_exists():
            return

        canvas.delete("all")
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 240)

        if not self._zone_sketch:
            # Only this show's own layout is ever drawn. Until it has been seen
            # once, say so plainly instead of rendering another venue's seats.
            canvas.create_text(
                width / 2, height / 2,
                text=self.NO_SKETCH_NOTICE, fill=MUTED, justify="center",
                width=max(200, width - 40),
            )
            self._zone_view = None
            return

        # Only blocks that are selling this round.
        #
        # Drawing the rest was an attempt to make this map look like the 예매 창
        # beside it, and it failed twice: as dimmed seats it read as a snapshot
        # of availability, and as dimmed blocks it read as a bug — "seat areas
        # are falsely getting picked up". They are neither. On 26007442 they are
        # two six-seat side strips and an eighty-seat block fifty units clear of
        # the house, none of it on sale, none of it in the visible map. You
        # cannot aim at a block that cannot sell, so a range picker that shows
        # them is offering a choice that does nothing. The count is named in the
        # hint instead.
        live = live_block_keys(self._zone_sketch)
        drawable = [row for row in self._zone_sketch if row.get("k") in live]
        view = project_venue(
            [{"block_key": row["key"], **row} for row in self._block_rows],
            drawable,
            width,
            height,
            # Draw the whole venue: this is what the area is dragged over, and a
            # seat that is not on screen cannot be selected.
            include_all=True,
        )
        self._zone_view = view

        # Size the dots to the venue's own seat pitch. A fixed radius drew a
        # dense house as a smear and a small one as scattered specks; NOL sizes
        # its circles to the spacing, which is what makes the two maps read as
        # the same room. The gap keeps rows legible instead of merging them.
        radius = max(1.0, min(6.0, seat_pitch(view.seats) * 0.40))

        rect = self._rect_tuple()
        for seat in view.seats:
            # The accent means exactly one thing: this seat will be watched.
            # No box drawn is not "nothing chosen" — it is "the whole house",
            # which is what the watch actually does, so the whole house is
            # accent. Draw a box and the accent narrows to it.
            inside = rect is None or (
                rect[0] <= seat.venue_x <= rect[2] and rect[1] <= seat.venue_y <= rect[3]
            )
            # Two states, because there are two: watched, or not. Everything
            # drawn here is on sale, so availability never enters into it —
            # the seats inside a range that are taken right now are precisely
            # what 취켓팅 is waiting for.
            colour = ACCENT if inside else SEAT_IDLE
            canvas.create_oval(
                seat.x - radius, seat.y - radius, seat.x + radius, seat.y + radius,
                fill=colour, outline="",
            )

        if view.stage:
            left, top, right, bottom = view.stage
            canvas.create_rectangle(left, top, right, bottom, outline=MUTED, dash=(3, 3))
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="STAGE", fill=MUTED)

        if rect is not None:
            x0, y0 = view.venue_to_canvas(rect[0], rect[1])
            x1, y1 = view.venue_to_canvas(rect[2], rect[3])
            canvas.create_rectangle(x0, y0, x1, y1, outline=GREEN, width=2, tags="watch")

        if self._zone_hint is not None:
            self._zone_hint.set(self._zone_hint_text())

    def _zone_drag_start(self, event) -> None:
        self._zone_drag = (event.x, event.y)

    def _zone_drag_move(self, event) -> None:
        if self._zone_drag is None or self._zone_canvas is None:
            return
        x0, y0 = self._zone_drag
        self._zone_canvas.delete("watch")
        self._zone_canvas.create_rectangle(
            x0, y0, event.x, event.y, outline=GREEN, width=2, tags="watch"
        )

    def _zone_drag_end(self, event) -> None:
        start, self._zone_drag = self._zone_drag, None
        view = self._zone_view
        if start is None or view is None:
            return
        x0, y0 = start
        # A click rather than a drag means "watch everywhere again" — the same
        # gesture people use to clear a selection anywhere else.
        if is_click(x0, y0, event.x, event.y):
            self._clear_watch_rect()
            return
        left, top, right, bottom = view.canvas_rect_to_venue((x0, y0, event.x, event.y))
        self._watch_rect = {"left": left, "top": top, "right": right, "bottom": bottom}
        self._push_seat_config()
        self._update_zone_summary()
        self._redraw_zone_map()

    def _clear_watch_rect(self) -> None:
        self._watch_rect = None
        self._push_seat_config()
        self._update_zone_summary()
        self._redraw_zone_map()

    def _selected_block_keys(self) -> list[str]:
        return [part.strip() for part in self.block_keys.get().split(",") if part.strip()]

    def _update_zone_summary(self) -> None:
        if self._watch_rect is None:
            self.zone_summary.set("감시 구역: 전체")
            return
        if self._zone_sketch:
            live = live_block_keys(self._zone_sketch)
            inside = [
                row for row in seats_in_watch_rect(self._zone_sketch, self._rect_tuple())
                if row.get("k") in live
            ]
            if not inside:
                # 0석 means the box caught nothing and will be ignored at run
                # time; saying "지정됨" for that reads as if it were working.
                self.zone_summary.set("감시 구역: 좌석 없음 — 다시 그어 주세요")
            else:
                self.zone_summary.set(f"감시 구역: 지정됨 · {len(inside)}석")
        else:
            self.zone_summary.set("감시 구역: 지정됨")


    def _set_open_time(self, open_kst: str) -> None:
        parts = open_kst.split(" ")
        if len(parts) == 2:
            self.target_date.set(parts[0])
            self.target_time.set(parts[1])

    def _apply_catalog_from_state(self) -> None:
        catalog = self.browser.read_show_catalog()
        if not catalog:
            self.after(800, self._apply_catalog_from_state)
            return
        self._apply_catalog(catalog)

    def _apply_catalog(self, catalog: dict) -> None:
        """Merge in-page data. Blocks only exist once a seat session is live."""
        incoming = catalog.get("blocks") or []
        previous = self._catalog or {}
        new_code = str(catalog.get("goods_code") or "")
        old_code = str(previous.get("goods_code") or "")
        self._catalog = catalog
        for key, var in (("goods_code", self.goods_code), ("place_code", self.place_code)):
            if catalog.get(key) and not var.get().strip():
                var.set(str(catalog[key]))
        # Follow the 예매판 round only while the user has not chosen one. Once
        # they pick from the 일정 list, that choice is the entry's round and the
        # poll must not walk it back — the catalog republishes four times a
        # second, and letting it win made the picker unusable.
        if show_changed_code := (new_code and old_code and new_code != old_code):
            self._round_user_picked = False
            self.rounds = []
        if not getattr(self, "_round_user_picked", False):
            if catalog.get("play_date"):
                self.play_date.set(str(catalog["play_date"]))
            if catalog.get("play_seq"):
                self.play_seq.set(str(catalog["play_seq"]))
        if catalog.get("play_time"):
            self.play_time.set(str(catalog["play_time"]))
        self._apply_rounds(catalog)
        self._refresh_round_line()

        grades = catalog.get("grades") or []
        if grades:
            self._merge_live_grades(grades)
        else:
            self._refresh_show_where()

        # A watch rect belongs to one show's coordinate space and to no other.
        # Seat positions have no common scale between shows — posTop spans
        # 52-111 on one venue and 1168-1183 on another — so a box kept across a
        # show change matches nothing and the run reports 후보 없음 forever. The
        # branch below only cleared it when the new show brought no blocks,
        # which is the case that does not matter.
        # What we are aiming at is a show *and* a round, never just a show.
        #
        # Block keys embed the 회차 — the same venue is 017:001 on one round and
        # 022:001 on the next — so a rect, a sketch or a block list kept across
        # a 일정 변경 points at seats that no longer exist, and both functions
        # quietly stop finding anything. Comparing only goods_code missed the
        # case entirely, because the code does not change with the round.
        new_seq = str(catalog.get("play_seq") or "")
        old_seq = str((previous or {}).get("play_seq") or "")
        show_changed = bool(new_code and old_code and new_code != old_code)
        round_changed = bool(new_seq and old_seq and new_seq != old_seq and not show_changed)
        target_changed = show_changed or round_changed

        if target_changed and self._watch_rect is not None:
            self._watch_rect = None
            self._update_zone_summary()
            self._note(
                "회차가 바뀌어 감시 구역을 해제했습니다"
                if round_changed
                else "공연이 바뀌어 감시 구역을 해제했습니다"
            )

        # The drawn seats belong to the old show for the same reason the rect
        # does. Clearing them only in the no-blocks branch below left the old
        # venue on screen whenever the new show's blocks arrived before its
        # sketch had been built — the picker showed the previous show's map.
        if target_changed:
            self._zone_sketch = []
            self._clear_zone_canvas()

        if incoming:
            self._apply_blocks(incoming)
            sketch = catalog.get("sketch")
            if isinstance(sketch, list) and sketch:
                self._zone_sketch = [row for row in sketch if isinstance(row, dict)]
                self._cache_sketch(new_code, self._zone_sketch, self._block_rows)
            self._schedule_zone_map()
        elif target_changed:
            self._watch_rect = None
            self._apply_blocks([])
            # No live blocks yet — the show is not open, or the map is not up.
            # The virtual seat map still needs a venue to draw, so fall back to
            # the layout cached the last time this show's seat map was seen.
            self._load_cached_sketch(new_code)
        else:
            sketch = catalog.get("sketch")
            if isinstance(sketch, list) and sketch:
                self._zone_sketch = [row for row in sketch if isinstance(row, dict)]
                self._cache_sketch(new_code, self._zone_sketch, self._block_rows)
                self._schedule_zone_map()
            elif not self._zone_sketch:
                self._load_cached_sketch(new_code)
            self._refresh_zone_picker()

    @staticmethod
    def _default_venue_blocks() -> list[dict]:
        """A generic 1F A/B/C + 2F frame, so the picker is never empty.

        Coordinates are in the same venue space the real sketch uses (small
        arbitrary units); they exist only to give the user something to drag a
        target over before this show's real layout has been seen.
        """
        return [
            {"key": "A", "name": "1F A블록", "left": 8, "top": 30, "right": 34, "bottom": 78},
            {"key": "B", "name": "1F B블록(중앙)", "left": 37, "top": 26, "right": 63, "bottom": 82},
            {"key": "C", "name": "1F C블록", "left": 66, "top": 30, "right": 92, "bottom": 78},
            {"key": "2F", "name": "2F", "left": 20, "top": 88, "right": 80, "bottom": 104},
        ]

    @classmethod
    def _default_venue_sketch(cls) -> list[dict]:
        """Seat dots filling the default blocks, for project_venue to draw."""
        sketch: list[dict] = []
        for block in cls._default_venue_blocks():
            x0, y0, x1, y1 = block["left"], block["top"], block["right"], block["bottom"]
            step_x = max(2, (x1 - x0) / 9)
            step_y = max(2, (y1 - y0) / 7)
            y = y0
            while y <= y1:
                x = x0
                while x <= x1:
                    sketch.append({"k": block["key"], "x": round(x, 1), "y": round(y, 1)})
                    x += step_x
                y += step_y
        return sketch

    def _sketch_cache_path(self, goods: str):
        """Where a venue layout is kept so the virtual map works before open.

        Keyed by goods code, panel-side (not the browser): the seat map is on
        tickets.interpark.com and its parked sketch lives in that origin's
        localStorage, unreachable from the nol.yanolja.com product page. Caching
        here makes the layout available for pre-open targeting from any page.
        """
        code = re.sub(r"[^A-Za-z0-9]", "", str(goods or ""))
        if not code:
            return None
        return self.browser.state_path.with_name(f".nolsniper_sketch_{code}.json")

    def _cache_sketch(self, goods: str, sketch: list, blocks: list) -> None:
        path = self._sketch_cache_path(goods)
        if path is None or not sketch:
            return
        try:
            payload = {"goods_code": str(goods), "sketch": sketch, "blocks": blocks,
                       "at": time.time()}
            if payload != getattr(self, "_sketch_cache_last", None):
                self._sketch_cache_last = payload
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001 - a cache write must never break the poll
            pass

    def _load_cached_sketch(self, goods: str) -> bool:
        """Draw the virtual map from the cached layout when none is live yet."""
        if self._zone_sketch:
            return False
        path = self._sketch_cache_path(goods)
        if path is None or not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a bad cache is simply no cache
            return False
        sketch = [row for row in (data.get("sketch") or []) if isinstance(row, dict)]
        if not sketch:
            return False
        self._zone_sketch = sketch
        blocks = data.get("blocks") or []
        if blocks and not self._block_rows:
            self._block_rows = [row for row in blocks if isinstance(row, dict)]
        self._schedule_zone_map()
        return True

    def _apply_blocks(self, blocks: list) -> None:
        """Fill the zone list from the show's own blocks."""
        rows = []
        for block in blocks:
            if isinstance(block, dict):
                key = str(block.get("block_key") or block.get("key") or "")
                name = str(block.get("label") or block.get("block_name") or block.get("name") or key)
                box = parse_box(block)
            else:
                key = name = str(block)
                box = None
            if not key:
                continue
            row = {"key": key, "name": name}
            if box:
                row["left"], row["top"], row["right"], row["bottom"] = box
            rows.append(row)
        self._block_rows = rows
        self._refresh_zone_picker()

    def _refresh_zone_picker(self) -> None:
        if self._zone_hint is not None:
            self._zone_hint.set(self._zone_hint_text())
        if not hasattr(self, "block_list") or not self.block_list.winfo_exists():
            self._update_zone_summary()
            return
        keep = {key for key in self._selected_block_keys()}
        self._block_selecting = True
        try:
            self.block_list.delete(0, tk.END)
            for row in self._block_rows:
                label = row["name"] if row["name"] == row["key"] else f"{row['name']} ({row['key']})"
                self.block_list.insert(tk.END, label)
            live = {row["key"] for row in self._block_rows}
            stale = keep - live
            if stale and not (keep & live):
                keep = set()
                self.block_keys.set("")
                self.block_names.set("")
                # Keep the drawn watch rect — block keys change with playSeq
                # (019:001 vs 001:001) and wiping it made 감시 시작 look like
                # "전체" again.
                self._note(f"이전 공연의 구역 설정을 지웠습니다 · 구역 {len(self._block_rows)}개 인식")
            elif self._block_rows:
                self._note(f"구역 {len(self._block_rows)}개 인식")
            for index, row in enumerate(self._block_rows):
                if row["key"] in keep:
                    self.block_list.selection_set(index)
        finally:
            self._block_selecting = False
        self._update_zone_summary()
        self._schedule_zone_map()

    def _schedule_zone_map(self) -> None:
        if self._zone_canvas is None or self._zone_drag is not None:
            return
        job = getattr(self, "_zone_redraw_job", None)
        if job:
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._zone_redraw_job = self.after_idle(self._redraw_zone_map)

    def _on_strategy_pick(self, _event=None) -> None:
        label = self.strategy_box.get()
        for key, text in self.STRATEGY_LABELS.items():
            if text == label:
                self.seat_strategy.set(key)
                break




    # How often the panel asks "did anything free anywhere?". One request, ~132ms
    # measured, against a whole-venue sweep that costs 17 requests and ~4.4s on a
    # 34-block house. Paced well below what the endpoint can take, because the
    # point is to spend less, not more.
    TRIGGER_POLL_MS = 500

    def start_catch(self) -> None:
        try:
            self._remember_press("감시 시작")
            self._push_seat_config(command="run_catch", clear_arm=True)
            # Short on purpose: this says the press was sent, and the 예매 창's
            # own status — which is the answer — is one poll behind it.
            self._flash(ACCENT, "감시 시작 요청됨",
                        "예매 창이 좌석맵을 지켜보기 시작합니다.", seconds=2.0)
            self._trigger_state = None
            self._trigger_on = True
            self._start_trigger_worker()
        except Exception as exc:
            self._flash(AMBER, "감시를 시작하지 못했습니다", str(exc))

    def _start_trigger_worker(self) -> None:
        """Watch the whole-venue remaining count for the page, which cannot.

        The feed is served from api-ticketfront.interpark.com with no
        Access-Control-Allow-Origin, so an in-page fetch cannot read it — the
        same wall the in-page clock sync hits on the Date header. Python has no
        such restriction, so the panel does the looking and pushes the verdict
        across the bridge.
        """
        if getattr(self, "_trigger_thread", None) and self._trigger_thread.is_alive():
            return
        self._trigger_thread = threading.Thread(target=self._trigger_worker, daemon=True)
        self._trigger_thread.start()

    def _trigger_worker(self) -> None:
        trigger_errors = 0
        while getattr(self, "_trigger_on", False):
            total = None
            hide = bool((self._show_info_data or {}).get("hide_remain_seat"))
            if not hide:
                try:
                    goods = parse_goods_code(self.goods_code.get())
                    grades, _, _ = fetch_round_remains(
                        goods,
                        self.play_date.get(),
                        place_code=self.place_code.get(),
                        play_seq=self.play_seq.get() or None,
                        timeout=3.0,
                    )
                    total = sum(grade.remain for grade in grades)
                except Exception:
                    # A failed look must leave the watch sweeping as before.
                    total = None
            self._trigger_state = next_trigger_state(
                self._trigger_state, total, hide_remain=hide
            )
            try:
                self.browser.push_trigger(self._trigger_state.to_mapping())
                trigger_errors = 0
            except Exception as exc:  # noqa: BLE001 - the watch must outlive one write
                # Swallowed entirely before, so a trigger that could never reach
                # the page looked exactly like one that was working: the watch
                # would quietly pay the full sweep for every look.
                trigger_errors += 1
                if trigger_errors in (1, 20):
                    self._ui(self._note,
                             f"잔여석 신호를 예매 창에 전달하지 못했습니다: {exc}", error=True)
            time.sleep(self.TRIGGER_POLL_MS / 1000)

    def _stop_trigger_worker(self) -> None:
        self._trigger_on = False
        self._trigger_state = None

    def arm(self, *, dry_run: bool = False) -> None:
        if self._login_required():
            self._flash(AMBER, "로그인 필요 — 세션이 없습니다", "예매 창에서 로그인한 뒤 다시 누르세요.")
            return
        # Acknowledge the press on the spot. Everything after this happens on a
        # worker thread and the first thing it does takes ~2s, so without this
        # the button looks inert for long enough to be pressed again.
        self.status.set("예약 준비 중…")
        self._start_worker(lambda: self._arm_worker(dry_run=dry_run))

    def _bump_test_time(self) -> None:
        """Push the test a minute out — the quick smoke test.

        The old control was a list of relative offsets (30초/1분/… 뒤), which
        cannot rehearse against a real open time. This is what that list was
        actually good for, kept as one button beside a real clock.

        It bumps from *now* whenever the field is already in the past, which is
        the normal state of it: the default is set to "a minute out" once, when
        the panel is built, and nothing refreshes it. Ten minutes into a session
        the field names a moment ten minutes gone, and bumping from the field
        moved it to nine minutes gone — so the button appeared to do nothing and
        [테스트 실행] answered 이미 지난 시각입니다 however many times it was
        pressed. One press should always produce a moment you can rehearse
        against.
        """
        now = datetime.now(KST).replace(tzinfo=None)
        try:
            when = datetime.strptime(self._test_time_text(), "%Y-%m-%d %H:%M:%S")
        except (ValueError, NolSniperError):
            when = now
        self._set_test_time(max(when, now) + timedelta(minutes=1))

    def _test_time_from_show(self) -> None:
        """Aim the rehearsal at this show's own 티켓 오픈."""
        opens = f"{self.target_date.get().strip()} {self.target_time.get().strip()}"
        try:
            when = datetime.strptime(opens.strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                when = datetime.strptime(opens.strip(), "%Y-%m-%d %H:%M")
            except ValueError:
                self._note("이 공연의 티켓 오픈 시각을 아직 모릅니다", error=True)
                return
        self._set_test_time(when)
        self._note(f"테스트 시각을 티켓 오픈({when:%H:%M:%S})에 맞췄습니다")

    def _set_test_time(self, when: datetime) -> None:
        self.test_date.set(when.strftime("%Y-%m-%d"))
        self.test_hour.set(when.strftime("%H"))
        self.test_minute.set(when.strftime("%M"))
        self.test_second.set(when.strftime("%S"))

    ENTRY_PAGES = {"nol", "goods"}
    # How far into the past a picked rehearsal time may sit before the press is
    # refused outright. Wide enough that choosing a moment two seconds out and
    # taking a moment to press is still a rehearsal, not a rejection.
    TEST_TIME_PAST_TOLERANCE_S = 2.0

    def _entry_page_problem(self) -> str:
        """Why an entry cannot run right now, or "" if it can."""
        page = str((self.browser.read_page_context() or {}).get("page") or "")
        if page in self.ENTRY_PAGES:
            return ""
        where = {
            "seat": "이미 좌석맵에 있습니다 — 들어갈 대기열이 없습니다.",
            "waiting": "이미 대기열에 있습니다.",
            "gates": "이미 대기열에 있습니다.",
        }.get(page)
        return where or "예매 창에서 공연 페이지를 먼저 여세요."

    def run_entry_probe(self) -> None:
        """Ask the 예매 창 what the entry would do, without doing any of it.

        Deliberately not gated on `_entry_page_problem`: "you are on the wrong
        page" is one of the answers this is for, and refusing to look would be
        the same shrug the panel already gave before an open.

        Publishes the arm first so the probe has a target and a 진입 보정 to
        report — with dry_run set, so nothing can fire even if the moment has
        already passed. The report itself comes back through the ordinary
        status poll and is drawn by `_render_entry_probe`.
        """
        self._entry_probe_pending = True
        self.test_result.set("진입 점검 중…")
        try:
            target_unix = parse_target_time(self._target_time_text(), target_tz=KST)
        except Exception:  # noqa: BLE001 - a probe must work with no open time set
            target_unix = time.time()
        try:
            payload = self._arm_payload(
                target_unix=target_unix,
                offset_seconds=(self.clock.sync_result.offset_seconds if self.clock.sync_result else 0.0),
                dry_run=True,
            )
            # push, not _publish_arm: that also moves the countdown onto this
            # target, and a probe must not repoint the clock you are watching.
            self.browser.push(arm=payload.to_mapping())
        except Exception as exc:  # noqa: BLE001 - report what is missing, then still look
            self._note(f"공연 정보가 아직 부족합니다: {exc}", error=True)
        self.browser.send_command("probe_entry")

    def _render_entry_probe(self, report: dict) -> None:
        """What 진입 점검 found, in plain Korean.

        Every line answers a question that previously had no answer short of
        arming a real entry and watching: which route, whether the button is
        there, whether it is live yet, and when the corrected fire lands.
        """
        route = str(report.get("route") or "")
        page = {
            "nol": "NOL 상품 페이지",
            "goods": "인터파크 상품 페이지",
            "seat": "좌석맵",
            "gates": "게이트",
            "waiting": "대기열",
        }.get(str(report.get("page") or ""), "알 수 없는 페이지")
        button = report.get("button") or {}
        clock = report.get("clock") or {}
        arm = report.get("arm") or {}
        queue = report.get("queue") or {}

        lines = [f"현재 페이지  {report.get('origin', '')} ({page})"]

        if route == "waiting-api":
            lines.append("진입 방식    대기열 API 호출")
            if queue:
                lines.append(
                    "             API 읽기 가능 · " + str(queue.get("answer") or "")
                    if queue.get("readable")
                    else "             API 읽기 실패 · " + str(queue.get("error") or "")[:60]
                )
        else:
            lines.append("진입 방식    예매하기 버튼 클릭")
            lines.append("             대기열 API는 이 주소에서 막혀 있습니다")

        if not button.get("found"):
            lines.append("예매하기     버튼 없음 — 로그인·본인인증을 확인하세요")
        elif button.get("pressable"):
            lines.append("예매하기     버튼 있음 · 지금 바로 누를 수 있음")
        elif not button.get("visible"):
            lines.append("예매하기     버튼 있음 · 화면에 보이지 않음")
        else:
            lines.append("예매하기     버튼 있음 · 지금은 비활성 (오픈 전이면 정상)")

        quality = {
            "boundary": "예매 서버에서 직접 측정",
            "host": "조작판이 측정한 보정 사용",
            "fallback": "보정 실패 — 기기 시계 그대로",
            "none": "아직 동기화 안 됨",
        }.get(str(clock.get("quality") or ""), str(clock.get("quality") or "?"))
        row = f"시계         {quality} ({clock.get('offsetMs', 0):+d}ms)"
        if abs(int(clock.get("jumpMs") or 0)) > 2000:
            row += f" · 기기 시계 {int(clock['jumpMs']) / 1000:+.0f}초 변경됨"
        lines.append(row)

        fire_at = arm.get("fireAtServerUnix") or 0
        if fire_at:
            when = datetime.fromtimestamp(float(fire_at), KST)
            lines.append(
                f"진입 보정    {int(arm.get('entryOffsetMs') or 0):+d} ms → "
                f"{when:%H:%M:%S}.{when.microsecond // 1000:03d}부터 시도"
            )

        blocked = int(report.get("blockedMs") or 0)
        if blocked > 0:
            lines.append(f"주의         접속 차단 중 — {blocked // 1000}초 남음")

        self.test_result.set("\n".join(lines))

    def run_entry_test(self) -> None:
        """Rehearse the open at a moment you choose.

        The whole of 오픈 대기 is one instant that either works or is lost, and
        the only way to find out used to be to be there for it. This arms the
        real entry — same clock sync, same scheduler, same request — against a
        moment you pick, so it can be watched and repeated.
        """
        problem = self._entry_page_problem()
        if problem:
            self._note(problem, error=True)
            return
        # Read the picked time here, on the UI thread. Inside the lambda it ran
        # on the worker, reading tk variables off the main thread — and a bad
        # value surfaced from a background exception rather than as an answer to
        # the press.
        try:
            wanted = self._test_time_text()
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self._note(str(exc), error=True)
            return
        # Answer a past time here, not two seconds later out of the worker.
        #
        # _arm_worker does check it, but only after syncing the clock — so the
        # commonest mistake of all, pressing 테스트 실행 against the stale default
        # time, cost a two-second wait and then "이미 지난 시각입니다", which says
        # what is wrong and not what to do about it. The local clock is plenty
        # to decide this: the panel's own offset is milliseconds and the
        # tolerance below is seconds. The authoritative check stays in the
        # worker, against the server clock.
        picked = datetime.strptime(wanted, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        late = (datetime.now(KST) - picked).total_seconds()
        if late > self.TEST_TIME_PAST_TOLERANCE_S:
            self._note(
                f"테스트 시각 {picked:%H:%M:%S}은(는) 이미 지났습니다 — "
                f"[+1분]을 눌러 앞으로 옮긴 뒤 다시 실행하세요.",
                error=True,
            )
            return
        self.status.set("테스트 준비 중…")
        self._start_worker(
            lambda: self._arm_worker(dry_run=False, target_text=wanted, test=True)
        )

    def _test_time_text(self) -> str:
        """The picked moment, in the exact shape parse_target_time accepts."""
        date_text = self.test_date.get().strip()
        datetime.strptime(date_text, "%Y-%m-%d")
        parts = []
        for var, label in ((self.test_hour, "시"), (self.test_minute, "분"),
                           (self.test_second, "초")):
            raw = var.get().strip()
            if not raw.isdigit():
                raise NolSniperError(f"{label}는 숫자여야 합니다")
            parts.append(int(raw))
        hour, minute, second = parts
        if hour > 23 or minute > 59 or second > 59:
            raise NolSniperError("시각이 올바르지 않습니다")
        return f"{date_text} {hour:02d}:{minute:02d}:{second:02d}"

    def _start_worker(self, target) -> bool:
        """Run `target` in the background, or say why it will not.

        This returned silently when a worker was already alive — and one always
        is for the first couple of seconds after launch, because the startup
        clock sync shares this slot and takes ~2s. A press in that window did
        nothing whatsoever: no thread, no message, no error. "대기 시작 doesn't
        even do anything" was often exactly that.
        """
        if self.worker and self.worker.is_alive():
            self._note("이전 작업이 끝나는 중입니다 — 잠시 후 다시 눌러 주세요.", error=True)
            return False
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        return True

    def _sync_worker(self) -> None:
        try:
            self._sync_now()
            self._ui(self.status.set, "시각 동기화 완료")
        except Exception as exc:
            self._ui(self.status.set, f"동기화 실패: {exc}")

    def _sync_now(self):
        # poll_seconds=0.005 asks for a 5ms spacing, and Windows' default timer
        # granularity is ~15.6ms — three times that, which widens the very
        # second-boundary bracket sync_tick exists to tighten and can stop it
        # ever meeting its own accuracy gate. timing_precision asks for a 1ms
        # timer for the duration and is a no-op on macOS. Wrapped here rather
        # than inside core/clock.py so core stays free of platform imports.
        with app_platform.timing_precision():
            return self.clock.sync_tick(
                self.SYNC_URL,
                sample_count=self.SYNC_SAMPLES,
                min_samples=2,
                max_wait_seconds=8.0,
                poll_seconds=0.005,
            )

    def _poll_show(self) -> None:
        try:
            self._keep_browser_alive()
            snapshot = self.browser.read_snapshot()
            health = self.browser.read_bridge_health()
            context = snapshot["context"] or None
            status = snapshot["status"]
            seat = status.get("seat") or {}

            line = bridge_line(health, context, seat)
            self.bridge.set(f"{line} · {self._update_note}" if self._update_note else line)
            self._last_arm_status = status.get("arm") or {}
            self._last_arm_cfg = snapshot.get("arm") or {}
            self._last_context = context or {}
            if context:
                self._follow_browser_show(context)
            self._update_guidance(context, seat)

            catalog = snapshot["catalog"] or None
            if not catalog:
                self._forget_show_off_page(context)
            if catalog:
                prev = self._catalog or {}
                new_blocks = catalog.get("blocks") or []
                old_blocks = prev.get("blocks") or []
                new_sketch = catalog.get("sketch") or []
                old_sketch = prev.get("sketch") or []
                new_grades = catalog.get("grades") or []
                grade_sig = tuple(
                    (str(row.get("name") or ""), int(row.get("remain") or 0))
                    for row in new_grades
                    if isinstance(row, dict)
                )
                prev_grade_sig = tuple(
                    (str(row.get("name") or ""), int(row.get("remain") or 0))
                    for row in (prev.get("grades") or [])
                    if isinstance(row, dict)
                )
                round_changed = (
                    catalog.get("play_date") != prev.get("play_date")
                    or catalog.get("play_seq") != prev.get("play_seq")
                    or catalog.get("play_time") != prev.get("play_time")
                )
                # A catalog that carries only a round list changes none of the
                # above — no fetched_at, blocks, sketch or grades — so the 66
                # rounds goods-info delivered never reached the picker and it sat
                # blank (측정: 엘리자벳). The list itself has to count.
                rounds_changed = self._rounds_signature(catalog) != self._rounds_signature(prev)
                if (
                    catalog.get("fetched_at") != prev.get("fetched_at")
                    or len(new_blocks) != len(old_blocks)
                    or len(new_sketch) != len(old_sketch)
                    or grade_sig != prev_grade_sig
                    or round_changed
                    or rounds_changed
                ):
                    self._apply_catalog(catalog)
            self._apply_autopilot_status(status, health)
            self._publish_panel_state()
        except Exception as exc:  # noqa: BLE001 - keep the poll alive
            self._note(f"브라우저 동기화 오류: {exc}", error=True)
        self.after(500, self._poll_show)

    def _publish_panel_state(self) -> None:
        """What the panel is showing, as a file — for support and for tests.

        The panel's own words (mode, banner, instruction, which button is on,
        the band, the scroll position) were only ever on screen. Written next
        to the bridge state whenever any of it changes, so a live run can be
        checked against what the user actually saw.
        """
        try:
            canvas = getattr(self, "_scroll_canvas", None)
            scroll = round(float(canvas.yview()[0]), 4) if canvas is not None else None
            snapshot = {
                "mode": getattr(self, "_mode", ""),
                "banner": self.mode_banner.get(),
                "guidance": self.guidance.get(),
                "action_note": self.action_note.get(),
                "status": self.status.get(),
                "reason": self.reason.get(),
                "open_note": self.open_note.get(),
                "round_note": self.round_note.get(),
                "countdown": self.countdown.get(),
                "buttons": {
                    "arm": "disabled" not in self.btn_arm.state(),
                    "enter": "disabled" not in self.btn_enter_now.state(),
                    "catch": "disabled" not in self.btn_catch.state(),
                },
                "primary": (
                    "arm" if str(self.btn_arm.cget("style")) == "Primary.TButton"
                    else "enter" if str(self.btn_enter_now.cget("style")) == "Primary.TButton"
                    else "catch" if str(self.btn_catch.cget("style")) == "Primary.TButton"
                    else ""
                ),
                "scroll": scroll,
                "show": self.show_title.get(),
                "rounds": len(getattr(self, "rounds", None) or []),
                "virtual_map_seats": len(getattr(self, "_zone_sketch", None) or []),
                "target_rect": self._watch_rect,
            }
            if snapshot != getattr(self, "_panel_state_last", None):
                self._panel_state_last = snapshot
                snapshot = {**snapshot, "at": time.time()}
                path = self.browser.state_path.with_name(".nolsniper_panel_state.json")
                path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001 - a diagnostic must never break the poll
            pass

    def _forget_show_off_page(self, context: dict | None) -> None:
        """Let go of the previous show once the 예매 창 has left it.

        The page publishes no catalog on NOL home, 오픈 예정 or the ticket home
        (see onShowPage in the autopilot). Keeping the old rounds in the picker
        there meant 지금 진입 could fire for a show the window was no longer on.
        Only the round list and the pinned pick are dropped; the code fields
        stay so the show can be reopened by URL.
        """
        if not context or not self.rounds:
            return
        url = str(context.get("url") or "")
        if context.get("goods_code") or "/onestop/" in url or context.get("page") != "other":
            return
        self.rounds = []
        self._round_user_picked = False
        self._catalog = None
        try:
            self.round_box.configure(values=[])
            self.round_box.set("")
        except Exception:  # noqa: BLE001 - a widget that is not built yet
            pass
        self.round_note.set("공연을 열면 날짜·회차가 여기에 나옵니다.")

    def _render_entry_result(self, arm: dict) -> None:
        """What the last entry actually did.

        This used to render only after a 테스트 실행 — `_entry_test_started` was
        set in exactly one place and gated the whole method — so a *real* 대기
        시작 published every one of these numbers across the bridge and the panel
        threw them away. Which is why "it fires way outside the expected time"
        could not be answered: the answer was being computed and discarded on
        every run that mattered.
        """
        # A refused arm never fires, so gating on `fired` alone meant the one
        # case you most need to see — "it did not even try, and here is why" —
        # showed nothing at all. A block refuses before firing.
        # Re-entry counts too. fireEntry called from maybeReenter never sets
        # `fired`, so a re-entry loop — the thing most worth seeing, because it
        # spends requests against a lockout-capable endpoint — rendered nothing
        # at all unless it happened to also raise an error.
        if (
            not arm.get("fired")
            and not str(arm.get("lastError") or "").strip()
            and not (arm.get("reentryTries") or 0)
        ):
            return

        lateness = arm.get("latenessMs")
        lines = []
        error = str(arm.get("lastError") or "").strip()
        # `route` first. `enteredVia` collapsed a BookSession POST and a DOM
        # click into one value, "book", rendered as "예매 창으로 진입" — which
        # is the one question this panel is asked most often ("what did it
        # actually press?") answered with a shrug. Kept as the fallback for a
        # 예매 창 running an older automation that publishes no route.
        via = {
            "waiting-api": "대기열 API로 진입",
            "book-session": "BookSession 폼으로 진입",
            "dom-click": "페이지의 예매하기 버튼을 눌러 진입",
            "dom-click-forced": "예매하기 버튼을 강제로 활성화해 진입",
            "sso-gate": "SSO 게이트 주소로 진입",
            "gates": "게이트 세션으로 진입",
            "dry-run": "발사만 확인 (요청 없음)",
        }.get(str(arm.get("route") or ""), "") or {
            "waiting": "대기열로 진입",
            "book": "예매 창으로 진입",
            "dry-run": "발사만 확인 (요청 없음)",
        }.get(str(arm.get("enteredVia") or ""), "")

        if error:
            # Long enough for a block message to keep the endpoint it names.
            lines.append(f"진입 {'실패' if arm.get('fired') else '안 함'} · {error[:90]}")
        elif via:
            lines.append(f"진입 성공 · {via}")
        else:
            lines.append("발사함 · 진입 확인 중")

        acquired = arm.get("acquiredLatenessMs")
        attempts = arm.get("waitingAttempts") or 0
        if isinstance(lateness, (int, float)):
            # Signed on purpose: early is as informative as late. The fire is
            # now deliberately early by ENTRY_LEAD_MS, so on its own this reads
            # ≈ -400ms every time — the number that moves is the one below it.
            row = f"발사 {lateness:+.0f}ms"
            if isinstance(acquired, (int, float)):
                row += f" · 대기열 확보 {acquired:+.0f}ms"
            if attempts:
                row += f" · 요청 {attempts}회"
            # The click's own lateness, which is the number the DOM route is
            # tuned by — the fire above it is deliberately early and barely
            # moves, so on its own it says nothing about how the open went.
            click_late = arm.get("clickLatenessMs")
            if isinstance(click_late, (int, float)):
                row += f" · 클릭 {click_late:+.0f}ms"
                if arm.get("clickTries"):
                    row += f" ({arm['clickTries']}회 확인)"
            lines.append(row)

        offset_ms = arm.get("entryOffsetMs") or 0
        if offset_ms:
            lines.append(f"진입 보정 {offset_ms:+d}ms 적용됨")

        jump_ms = arm.get("clockJumpMs") or 0
        if jump_ms:
            lines.append(
                f"대기 중 기기 시계가 {jump_ms / 1000:+.0f}초 바뀌었습니다 — 발사 시각은 유지됨"
            )

        # Where the time actually went. "It takes too long" is otherwise a
        # feeling; these are the three numbers that make it a measurement.
        sync_ms = arm.get("syncMs") or 0
        enter_ms = arm.get("enterMs") or 0
        if sync_ms or enter_ms:
            lines.append(f"시각 맞추기 {sync_ms}ms · 진입 {enter_ms}ms")

        quality = str(arm.get("clockQuality") or "")
        if quality:
            source = {
                "boundary": "예매 서버에서 직접 측정",
                "host": "조작판이 측정한 보정 사용",
                "fallback": "보정 실패 — 기기 시계 그대로",
                "none": "동기화 안 됨",
            }.get(quality, quality)
            lines.append(f"시계 {source} ({arm.get('clockOffsetMs') or 0:+d}ms)")

        # The round it used. A test that says 회차 017 while the 예매 창 shows 022
        # has found the problem for you, which is most of why this exists.
        goods = str(arm.get("goodsCode") or "").strip()
        seq = str(arm.get("playSeq") or "").strip()
        if goods or seq:
            lines.append(f"상품 {goods or '?'} · 회차 {seq or '?'}")

        # Published since the feature existed and drawn nowhere, which is how a
        # 400ms re-entry loop stayed invisible.
        reentries = arm.get("reentryTries") or 0
        if reentries:
            lines.append(f"재진입 {reentries}회 — 진입이 되돌려질 때마다 다시 시도합니다")

        host = str(arm.get("queueHost") or "").strip()
        if host:
            lines.append(f"대기열 호스트 {host} · 다음 진입부터 미리 연결")

        # What the queue endpoint actually said either side of the open. This is
        # the record that decides whether polling across the boundary is even
        # the right method.
        lines.extend(waiting_log_lines(arm))
        # And what the 예매하기 button was doing either side of the open. On a
        # NOL product page this is the only record there is — the queue burst
        # never runs from that origin.
        lines.extend(click_log_lines(arm))

        self.test_result.set("\n".join(lines))

    def _apply_autopilot_status(self, status: dict | None = None,
                                health: dict | None = None) -> None:
        """Mirror what the 예매 창 is doing — every tick, whatever it says.

        A renderer and nothing more: `live_state` decides, this puts it on
        screen. It used to decide here as well, and it opened with

            if not message: return

        `seatState.message` is "" on every fresh injection of the autopilot and
        is only ever written by `updateOverlay`, so a page that had gone quiet —
        a reload after a cancelled seat, a stopped run, a window that closed —
        published a truthful "I am idle" and the panel *discarded it* and went
        on repainting its last frame. That is the 좌석 잡음 that never clears,
        and it is the opposite of watching the 예매 창.
        """
        if status is None:
            status = self.browser.read_autopilot_status() or {}
        if health is None:
            health = self.browser.read_bridge_health()
        seat = status.get("seat") or {}
        # A probe answer wins over the last entry result: the probe is what the
        # user just pressed, and letting a stale entry line overwrite it on the
        # next 500ms poll would make the button look broken. Latched on the
        # report's own timestamp so it is drawn once and then released.
        probe = seat.get("entryProbe")
        if isinstance(probe, dict) and getattr(self, "_entry_probe_pending", False):
            if probe.get("at") != getattr(self, "_entry_probe_at", None):
                self._entry_probe_at = probe.get("at")
                self._entry_probe_pending = False
                self._render_entry_probe(probe)
        else:
            self._render_entry_result(status.get("arm") or {})

        # Once a seat session exists the bitmap is the truthful count, so the
        # show table switches from the API's `remain` to what is actually free.
        free = seat.get("freeSeats")
        by_grade = seat.get("freeByGrade")
        if not isinstance(by_grade, dict):
            by_grade = None
        if self._grade_rows and ((isinstance(free, int) and free > 0) or by_grade):
            self._render_seat_table(
                self._grade_rows,
                bool((self._show_info_data or {}).get("hide_remain_seat")),
                live_free=free if isinstance(free, int) and free > 0 else None,
                free_by_grade=by_grade,
            )


        tone, headline, why = live_state(seat, health, asked=self._pending_press(seat))
        # A read that failed once or twice is a document swap in progress, not
        # news: keep the band on what it said until the page answers again.
        if int((health or {}).get("failures") or 0) in (1, 2) and bridge_status(health or {})[0] == "failing":
            return
        # A flash is a message about something the user just did, and the band
        # is repainted twice a second — so it has to be held or it is not shown
        # at all. A caught seat is the one thing nothing may sit on top of.
        colour = TONES.get(tone, FAINT)
        if colour != GREEN and time.monotonic() < self._flash_until:
            return
        self._draw_band(colour, headline, why)

    def _draw_band(self, colour: str, headline: str, why: str) -> None:
        """Draw the band without flapping.

        A text-only change has to be seen on two consecutive polls (≈1s)
        before it is drawn; a mode change, a held seat or a fault is drawn at
        once. The seat page republishes its message four times a second and
        two consecutive polls rarely agreed word for word, which is what made
        the band flicker between 좌석 잡음 and 대기 중.
        """
        key = (colour, headline, why)
        mode = getattr(self, "_mode", "")
        mode_changed = mode != getattr(self, "_band_mode", None)
        urgent = colour in (GREEN, AMBER) or mode_changed
        if key != self._band_drawn and not urgent and key != self._band_candidate:
            self._band_candidate = key
            return
        self._band_candidate = key
        self._band_drawn = key
        self._band_mode = mode
        self._set_state(colour, headline, why)

    @staticmethod
    def _press_signature(seat: dict) -> tuple:
        """What moves when the 예매 창 acts on anything at all.

        Any one of these changing is proof the page is alive and received the
        command; none of them moving means nothing was there to receive it.
        """
        return (
            bool(seat.get("running")),
            int(seat.get("traceLen") or 0),
            str(seat.get("lastExit") or ""),
            str(seat.get("lastError") or ""),
            str(seat.get("message") or ""),
        )

    def _remember_press(self, label: str) -> None:
        seat = (self.browser.read_autopilot_status() or {}).get("seat") or {}
        self._asked = (label, time.monotonic(), self._press_signature(seat))

    def _pending_press(self, seat: dict) -> tuple[str, float] | None:
        """A press the 예매 창 has shown no sign of receiving.

        `browser_host.apply_state` runs every command as
        `window.NOLSniper && NOLSniper.runCatch()` and then drops it from the
        state file whether or not there was anything there to run it. So a
        press against a page the autopilot has not been injected into is
        swallowed in silence — the button depresses, the panel says what it
        hoped would happen, and nothing anywhere records that it did not.
        """
        if self._asked is None:
            return None
        label, at, signature = self._asked
        if self._press_signature(seat) != signature:
            self._asked = None
            return None
        return label, time.monotonic() - at

    def _note(self, text: str, *, error: bool = False) -> None:
        """A one-off message the user needs to see.

        These all used to go to `log_text` — a StringVar bound to no widget. Two
        dozen call sites wrote to it, including every error path in the arm
        worker, the browser sync and the remain refresh, so a failed 대기 시작
        reported itself somewhere nobody could read. They land on the live line
        now, and errors take the dot amber with them.
        """
        # The dot carries the state so the text does not have to spell it out.
        # Errors are flashed — they are about something that just went wrong and
        # the next poll is 500ms away. Progress chatter is not: it is worth less
        # than a live watch's own status and must never sit on top of it.
        if error:
            self._flash(AMBER, "문제가 발생했습니다", text)
        else:
            self.reason.set(text)

    def _flash(self, colour: str, headline: str, why: str, seconds: float = 6.0) -> None:
        """A message about something that has just happened, held long enough
        to read.

        The band is repainted from the 예매 창 every 500ms, so anything written
        straight to it — 정지 요청됨, a lookup that failed, a config file that
        would not parse — used to survive exactly one tick, which is the same
        as never being shown. `_apply_autopilot_status` steps around a live
        flash; a caught seat still goes through it.
        """
        self._set_state(colour, headline, why)
        self._flash_until = time.monotonic() + seconds

    def _set_state(self, colour: str, headline: str, why: str) -> None:
        self.status.set(headline)
        self.reason.set(why)
        self._state_colour = colour
        dot = getattr(self, "status_dot", None)
        if dot is not None and dot.winfo_exists():
            dot.configure(fg=colour)





    def _browser_goods_code(self, context: dict | None) -> str:
        raw = (context or {}).get("goods_code")
        if not raw:
            return ""
        try:
            return parse_goods_code(str(raw))
        except Exception:
            return str(raw).upper()

    def _follow_browser_show(self, context: dict) -> None:
        """When the user opens a show in the browser, fill this panel to match.

        Only on a *change* of show or round. The page is read every 500ms and
        these are editable fields — re-applying them every tick would overwrite
        a round the user picked by hand a moment earlier.
        """
        code = self._browser_goods_code(context)
        if not code:
            return

        if code != self._auto_loaded_code:
            if code == self._fetching_code:
                return
            # _auto_loaded_code is claimed only once the lookup succeeds, in
            # _apply_show_info. Setting it here meant one failed fetch left the
            # panel at "공연 정보를 가져오는 중…" with every button disabled and
            # nothing ever trying again.
            self._fetching_code = code
            self._followed_round = None
            self._remain_refresh_key = None
            # A new show starts with no round: the old play_date/seq/time belong
            # to the show we are leaving, and _apply_context_fields only *sets*
            # fields the incoming context carries — so a product page with no
            # round left the previous show's 회차 on the card (측정: 디어 에반
            # 핸슨이 회차 004로 표시). Clear them, then fill from the new show.
            self._round_user_picked = False
            self.rounds = []
            for var in (self.play_date, self.play_seq, self.play_time):
                var.set("")
            self.show_round.set("")
            self._refresh_show_where()
            # The virtual seat map is keyed strictly by goods code: the previous
            # show's sketch is meaningless here (seat coords have no common
            # scale between venues), so drop it and load this show's cached
            # layout at once. The picker window, if open, redraws from it.
            self._zone_sketch = []
            self._block_rows = []
            self._catalog = None
            self._watch_rect = None
            self._clear_zone_canvas()
            self._load_cached_sketch(code)  # else the canvas shows NO_SKETCH_NOTICE
            self._schedule_zone_map()
            self.goods_code.set(code)
            self.product_url.set(f"https://nol.yanolja.com/ticket/products/{code}")
            # Refresh the card the instant the show changes. The title was only
            # ever written when the lookup finished, so a slow or failed fetch
            # left the previous show's name on screen (측정: 김주택 shown as
            # 드라큘라). Say what is being loaded until the real name lands.
            self.show_title.set(f"불러오는 중… · {code}")
            self.show_where.set("")
            self.show_round.set("")
            self._show_info_data = None
            self._grade_rows = []
            self._render_seat_table([], False)
            if not getattr(self, "_armed_target_unix", None):
                self.status.set(f"예매 창 공연 감지 · {code}")
            self._apply_context_fields(context)
            # The page itself carries no round list and no open time; the
            # ticketfront API has both, so the panel is filled from there.
            self.fetch_show(navigate=False)
            return

        # Same show, but the user moved to another date or round in the page.
        round_key = (
            context.get("play_date"),
            context.get("play_seq"),
            context.get("play_time"),
        )
        if any(round_key) and round_key != self._followed_round:
            self._followed_round = round_key
            self._apply_context_fields(context)
            self._select_round_matching(context)
            self._schedule_remain_refresh(context)

    def _select_round_matching(self, context: dict) -> None:
        """Move the ① picker to the round the page just showed.

        Clicking a date or time block on the product page changes the page's
        own selection; the picker used to ignore it and stay on whatever it
        seeded, so the calendar and the combobox disagreed (측정: 엘리자벳).
        A round the user pinned by hand is left alone.
        """
        rounds = getattr(self, "rounds", None) or []
        if not rounds or getattr(self, "_round_user_picked", False):
            return
        date = re.sub(r"\D", "", str(context.get("play_date") or ""))
        time_ = re.sub(r"\D", "", str(context.get("play_time") or ""))
        seq = str(context.get("play_seq") or "").strip()
        best = -1
        for i, row in enumerate(rounds):
            if seq and str(row.get("play_seq") or "") == seq:
                best = i; break
            if date and re.sub(r"\D", "", str(row.get("play_date") or "")) == date:
                if not time_ or re.sub(r"\D", "", str(row.get("play_time") or "")) == time_:
                    best = i
                    if time_: break
        if best >= 0 and hasattr(self, "round_box"):
            try:
                self.round_box.current(best)
            except Exception:  # noqa: BLE001 - a stand-in without a box
                pass
            self._on_round_pick()

    def _refresh_round_line(self) -> None:
        """The 일정 the macro is targeting, on screen.

        It changes underneath everything else — every block key the macro uses
        embeds it — and until now nothing showed it, so a panel aimed at the
        wrong round looked identical to one aimed at the right one.
        """
        date = self._pretty_play_date()
        parts = [part for part in (date, self._clock_text(self.play_time.get())) if part]
        seq = self.play_seq.get().strip()
        if seq:
            parts.append(f"회차 {seq}")
        self.show_round.set(" · ".join(parts))

    def _apply_context_fields(self, context: dict) -> None:
        for key, var in (
            ("place_code", self.place_code),
            ("play_date", self.play_date),
            ("play_seq", self.play_seq),
            ("play_time", self.play_time),
        ):
            value = context.get(key)
            if value:
                var.set(str(value))
        if context.get("ticket_open") and not self._show_info_data:
            self._set_open_time(str(context["ticket_open"]))
        self._refresh_show_where()

    def _latch_enable(self, key: str, want: bool) -> bool:
        """Debounce a button's enabled state.

        The show state arrives on a 500ms poll that occasionally drops a frame,
        and enabling straight off it made [오픈에 자동 진입] flicker. This keeps
        the current value until the opposite has been asked for on two
        consecutive polls, so a lone glitch is absorbed.
        """
        latch = getattr(self, "_enable_latch", None)
        if latch is None:
            latch = self._enable_latch = {}
        state, streak = latch.get(key, (want, 0))
        if want == state:
            latch[key] = (state, 0)
            return state
        streak += 1
        if streak >= 2:
            latch[key] = (want, 0)
            return want
        latch[key] = (state, streak)
        return state

    def _update_guidance(self, context: dict | None, seat: dict | None = None) -> None:
        """Say what to do next, and only enable the buttons that can work.

        Which step you are on is decided by the page the browser is actually
        showing, so the panel cannot disagree with the window next to it.

        The division with the live band is that this is the only thing on
        screen addressed to *you* — the next thing to press — while the band
        reports what the macro is doing. That held right up until the macro
        started doing something: this read `page` and nothing else, so the
        moment you reached a seat map it said "[감시 시작]을 누르면…" and went
        on saying it while the watch ran, while a seat was held, forever. Two
        boxes then described the same moment and one of them was telling you to
        press a button you had already pressed. A step you have taken is not
        guidance, so once the macro is working this gets out of the way.
        """
        page = str((context or {}).get("page") or "")
        url = str((context or {}).get("url") or "")
        seat = seat or {}
        arm = getattr(self, "_last_arm_status", None) or {}
        loaded = self._show_info_data
        goods_on_page = self._browser_goods_code(context)
        try:
            health = self.browser.read_bridge_health() or {}
            bridge_state = bridge_status(health)[0]
            # One failed read during a navigation is not "offline": the page
            # is between documents for a poll or two. Lost or never-started is.
            bridge_live = bridge_state == "live" or (
                bridge_state == "failing" and int(health.get("failures") or 0) < 3
            )
        except Exception:  # noqa: BLE001 - no bridge yet is "not live", not a crash
            bridge_live = False
        mode = derive_mode(page=page, url=url, seat=seat, arm=arm, bridge_live=bridge_live)
        # A glitchy 500ms poll — page momentarily "other", context with no
        # goods — must not drop a detected show. If we have a loaded show and
        # the bridge is live, an incoming no_show/offline is treated as noise
        # and the last real mode is kept until the drop is confirmed.
        loaded_now = bool(self._show_info_data) or bool(goods_on_page) or bool(getattr(self, "_auto_loaded_code", ""))
        if mode in {"no_show", "offline"} and loaded_now and bridge_live:
            mode = getattr(self, "_mode", mode)
        # Hysteresis on the way *down*: a quiet mode (nothing is happening) must
        # be seen on MODE_CONFIRM_POLLS consecutive polls before it replaces an
        # active one, so a document swap mid-entry never flashes 공연 없음.
        # Active modes are drawn at once — a held seat must never wait.
        quiet = {"no_show", "ready", "offline", "on_seat"}
        previous = getattr(self, "_mode", "no_show")
        MODE_CONFIRM_POLLS = 3
        if mode in quiet and mode != previous and previous not in quiet:
            if getattr(self, "_mode_candidate", None) == mode:
                self._mode_candidate_n = getattr(self, "_mode_candidate_n", 1) + 1
            else:
                self._mode_candidate = mode
                self._mode_candidate_n = 1
            if self._mode_candidate_n < MODE_CONFIRM_POLLS:
                mode = previous
            else:
                self._mode_candidate = None
                self._mode_candidate_n = 0
        else:
            self._mode_candidate = None
            self._mode_candidate_n = 0
        opens = self._open_time()
        phase = sale_phase(opens, datetime.now(KST))
        open_text = opens.strftime("%m-%d %H:%M") if opens else ""
        armed_at = getattr(self, "_armed_target_unix", None)
        if not armed_at:
            armed_at = (getattr(self, "_last_arm_cfg", None) or {}).get("target_server_unix")
        if mode == "armed" and armed_at:
            # The moment this arm fires, not the show's own open: a rehearsal
            # a minute out must not be labelled with a date months ago.
            open_text = datetime.fromtimestamp(float(armed_at), KST).strftime("%m-%d %H:%M:%S")
        rounds = getattr(self, "rounds", None) or []
        round_picked = bool(rounds) and bool(str(self.play_seq.get() or "").strip())
        reason = str(seat.get("lastError") or arm.get("lastError") or "")
        auto_seats = bool(self.auto_start_on.get()) if hasattr(self, "auto_start_on") else True
        told = mode_guidance(mode, phase, round_picked=round_picked, auto_seats=auto_seats,
                             open_text=open_text, reason=reason)
        instruction = told.instruction
        primary = told.primary
        logged_out = (context or {}).get("logged_in") is False
        if logged_out and mode in {"ready", "halted", "error", "no_show"}:
            # Real-time login watchdog: a page with no session cannot arm or
            # enter, and saying so beats a 401 at the open.
            told = told.__class__("[로그인 필요 — 세션이 없습니다]",
                                  "예매 창에서 로그인하세요. 로그인되면 버튼이 다시 켜집니다.",
                                  "", "로그인 필요", "로그인 필요")
            instruction = told.instruction
            primary = ""
        if mode == "ready" and not loaded:
            instruction = (f"예매 창에서 {goods_on_page}를 감지했습니다. 공연 정보를 가져오는 중…"
                           if goods_on_page else instruction)
            primary = ""
        # No legacy hard-block: some reserved-seat shows report the old engine
        # flag yet enter through onestop perfectly well (측정: 드라큘라). The
        # buttons stay live for every reserved-seat show; the flow note, if any,
        # is a hint, not a gate.
        self._mode = mode
        self.mode_banner.set(told.banner)
        self.mode_text.set(MODE_LABELS.get(mode, mode))
        self.guidance.set(f"지금 할 일 — {instruction}")
        # Buttons read the same answer. Exactly one of the two entry buttons is
        # the primary, and the other says why it is off.
        idle = mode in {"ready", "halted", "error"} and bool(loaded) and primary != ""
        arm_on = idle and not told.arm_reason
        enter_on = idle and not told.enter_reason
        # Latch each button: it changes state only after the new value has held
        # for two consecutive polls, so a single dropped poll cannot make
        # [오픈에 자동 진입] flicker enabled↔disabled twice a second.
        self._set_enabled(self.btn_arm, self._latch_enable("arm", arm_on))
        self._set_enabled(getattr(self, "btn_enter_now", None),
                          self._latch_enable("enter", enter_on))
        self._style_button(self.btn_arm, primary == "arm")
        self._style_button(getattr(self, "btn_enter_now", None), primary == "enter")
        notes = []
        if told.arm_reason:
            notes.append(f"오픈에 자동 진입 — {told.arm_reason}")
        if told.enter_reason:
            notes.append(f"지금 진입 — {told.enter_reason}")
        self.action_note.set("\n".join(notes))
        can_enter = page in {"nol", "goods"}
        self._set_enabled(getattr(self, "btn_test", None), can_enter)
        self._set_enabled(self.btn_catch, mode in {"on_seat", "halted"} or (page == "seat" and mode == "error"))
        self._style_button(self.btn_catch, primary == "catch")
        if mode in {"no_show", "offline"} or not loaded:
            self.open_note.set("공연을 열면 오픈 예정인지, 판매 중인지 여기에 표시됩니다.")
        elif phase == OPEN:
            self.open_note.set("판매 중 — 이미 열린 공연이라 기다릴 것이 없습니다. [지금 진입]으로 들어갑니다.")
        elif phase == BEFORE_OPEN:
            self.open_note.set(f"오픈 예정 {open_text} — [오픈에 자동 진입]을 누르면 오픈 순간 자동으로 들어갑니다.")
        else:
            self.open_note.set("오픈 시각 미확인 — ②에 시각을 넣거나, 이미 열렸으면 [지금 진입]을 누르세요.")
        self._show_guidance(True)

    @staticmethod
    def _style_button(widget, primary: bool) -> None:
        """Filled for the one thing to press, ghost for everything else."""
        if widget is None:
            return
        try:
            widget.configure(style="Primary.TButton" if primary else "CardGhost.TButton")
        except Exception:  # noqa: BLE001 - a stand-in without styles
            pass


    def _show_guidance(self, visible: bool) -> None:
        """Take the whole tip box away, not just its text — an empty bordered
        panel is louder than no panel."""
        # Kept for its callers; the box stays where it is. Packing it away
        # while the macro ran shoved every card up by its height, and the mode
        # banner now says what the macro is doing instead.
        del visible

    def _open_time(self) -> datetime | None:
        """When this show goes on sale, or None if we do not know."""
        raw = str((self._show_info_data or {}).get("ticket_open_kst") or "")
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        except ValueError:
            return None

    def _sale_open(self) -> bool:
        """True when the ticket-open time has already passed.

        An unknown open time is NOT "already open". It used to return True in
        both of those cases, which read as "no waiting needed" and disabled
        대기 시작 — the exact opposite of what an unknown time means for someone
        sitting in front of a show that has not opened. And the panel lets you
        type the time by hand, so locking the button was never necessary.
        """
        opens = self._open_time()
        if opens is None:
            return False
        return datetime.now(KST) >= opens

    @staticmethod
    def _set_enabled(widget, enabled: bool) -> None:
        if widget is not None:
            widget.state(["!disabled"] if enabled else ["disabled"])

    def _resolved_goods(self) -> str:
        raw = self.goods_code.get().strip()
        if not raw:
            context = self.browser.read_page_context()
            if context and context.get("goods_code"):
                raw = str(context["goods_code"])
        if not raw:
            raise NolSniperError("상품코드가 없습니다. 공연 페이지를 열고 정보를 가져오세요")
        try:
            return parse_goods_code(raw)
        except Exception:
            return raw.upper()

    def _arm_payload(self, *, target_unix: float, offset_seconds: float, dry_run: bool,
                     auto_offset_ms: int | None = None) -> ArmPayload:
        play_date = self.play_date.get().strip().replace("-", "")
        play_seq = self.play_seq.get().strip() or "001"
        play_time = re.sub(r"\D", "", self.play_time.get().strip())
        goods_code = self._resolved_goods()
        # The picked round is the one statement of date and time. play_date has
        # other writers (the API's first date, a remain refresh, the page), and
        # at the 2026-09-04 12:00 open the arm carried the *open* date with the
        # round's seq — so the seat map was asked to change to a day that had
        # no performance. Resolve both from the round list by seq instead.
        rounds = getattr(self, "rounds", None) or []
        chosen = next((row for row in rounds
                       if str(row.get("play_seq") or row.get("playSeq") or "") == play_seq), None)
        if chosen is None and rounds and hasattr(self, "round_box"):
            try:
                index = self.round_box.current()
                if 0 <= index < len(rounds):
                    chosen = rounds[index]
            except Exception:  # noqa: BLE001 - a stand-in without a box
                chosen = None
        if chosen is not None:
            play_seq = str(chosen.get("play_seq") or chosen.get("playSeq") or play_seq)
            if self._row_date(chosen):
                play_date = self._row_date(chosen)
            if self._row_time(chosen):
                play_time = self._row_time(chosen)
        if not play_date.isdigit() or len(play_date) != 8:
            raise NolSniperError("공연일은 YYYYMMDD 형식이어야 합니다")
        return ArmPayload(
            enabled=True,
            goods_code=goods_code,
            play_date=play_date,
            play_seq=play_seq,
            target_server_unix=target_unix,
            offset_seconds=offset_seconds,
            dry_run=dry_run,
            fired=False,
            use_waiting_api=True,
            place_code=self.place_code.get().strip(),
            channel_code="pc",
            pre_sales="N",
            auto_seats_after_entry=self.auto_start_on.get(),
            # A typed correction wins; an untouched 0 takes the RTT-derived lead.
            entry_offset_ms=(auto_offset_ms if auto_offset_ms is not None and self._entry_offset_ms() == 0
                             else self._entry_offset_ms()),
            play_time=play_time,
        )

    def _toggle_advanced(self) -> None:
        opening = not self.advanced_open.get()
        self.advanced_open.set(opening)
        self.btn_advanced.configure(text="고급 설정 ▾" if opening else "고급 설정 ▸")
        if opening:
            self.advanced_box.pack(fill="x")
        else:
            self.advanced_box.pack_forget()

    @staticmethod
    def _row_date(row: dict) -> str:
        """The round's performance date, whatever the API called it.

        Only the round's own date: play_date / playDate. The show-level start
        date (play_start_date / playStartDate) is the premiere, not this
        round — reading it here is what sent the schedule step hunting for
        20260804 when the user had picked 20260904 (측정: 엘리자벳).
        """
        for key in ("play_date", "playDate"):
            value = re.sub(r"\D", "", str((row or {}).get(key) or ""))
            if len(value) == 8:
                return value
        return ""

    @staticmethod
    def _row_time(row: dict) -> str:
        for key in ("play_time", "playTime"):
            value = re.sub(r"\D", "", str((row or {}).get(key) or ""))
            if value:
                return value
        return ""

    @staticmethod
    def _round_label(row: dict[str, str]) -> str:
        """One round, written the way a ticket buyer reads one.

        `1회차` on its own is the app's word, not the user's — the date and the
        clock time are what someone is actually choosing between.
        """
        date = NolSniperApp._row_date(row)
        clock = NolSniperApp._row_time(row)
        seq = str(row.get("play_seq") or row.get("playSeq") or "")
        when = f"{int(date[4:6])}월 {int(date[6:8])}일" if len(date) == 8 else date
        day = str(row.get("day_of_week") or "")
        korean_day = {
            "Mon": "월", "Tue": "화", "Wed": "수", "Thu": "목",
            "Fri": "금", "Sat": "토", "Sun": "일",
        }.get(day, "")
        if korean_day:
            when += f" ({korean_day})"
        if len(clock) >= 4:
            when += f" {clock[:2]}:{clock[2:4]}"
        return f"{when}  ·  {seq}회차" if seq else when

    def _fetch_rounds_fallback(self, goods: str, place: str) -> None:
        """Worker: fill the 일정 picker from goods-info when the page has not."""
        try:
            rounds, open_date = fetch_goods_info_rounds(goods, place)
        except Exception as exc:  # noqa: BLE001 - a fallback that fails is just no fallback
            self._ui(self._note, f"회차 조회 실패: {exc}")
            return
        if not rounds:
            return
        def apply() -> None:
            if getattr(self, "rounds", None):
                return  # the page got there first; its list wins
            if str(self.goods_code.get() or "") not in ("", str(goods)):
                return  # the show changed while we were fetching
            self._apply_rounds({"goods_code": goods, "place_code": place,
                                "rounds": rounds, "ticket_open_date": open_date})
            self._refresh_round_line()
        self._ui(apply)

    def _rounds_signature(self, catalog: dict | None) -> tuple:
        """What identifies a round list: its length and its first/last seq."""
        rounds = [row for row in ((catalog or {}).get("rounds") or []) if isinstance(row, dict)]
        if not rounds:
            return (0, "", "")
        seq = lambda row: str(row.get("play_seq") or row.get("playSeq") or "")  # noqa: E731
        return (len(rounds), seq(rounds[0]), seq(rounds[-1]))

    def _apply_rounds(self, catalog: dict) -> None:
        """Fill the picker from what the page published, without fighting the user.

        A selection already made is kept across polls: the catalog is republished
        four times a second and resetting the box on each one would make the
        picker impossible to use.
        """
        rounds = [row for row in (catalog.get("rounds") or []) if row.get("play_seq") or row.get("playSeq")]
        if not rounds:
            if not self.rounds:
                self.round_note.set("공연을 열면 날짜·회차가 여기에 나옵니다.")
            return
        labels = [self._round_label(row) for row in rounds]
        if labels == [self._round_label(row) for row in self.rounds]:
            return  # unchanged; leave the current selection alone
        self.rounds = rounds
        self.round_box.configure(values=labels)
        self.round_note.set(f"{len(rounds)}개 회차 · 하나를 고르세요.")

        # Pre-select whatever the panel already had, so a show reopened with the
        # same round does not silently move to a different one.
        wanted = str(self.play_seq.get() or "").strip()
        chosen = next((i for i, row in enumerate(rounds)
                       if str(row.get("play_seq") or row.get("playSeq") or "") == wanted), 0)
        self.round_box.current(chosen)
        self._on_round_pick()

        open_date = re.sub(r"\D", "", str(catalog.get("ticket_open_date") or ""))
        if len(open_date) == 14:
            self.target_date.set(f"{open_date[:4]}-{open_date[4:6]}-{open_date[6:8]}")
            self.target_time.set(f"{open_date[8:10]}:{open_date[10:12]}:{open_date[12:14]}")

    def _on_round_pick(self, event=None) -> None:
        """The picked round becomes the one the entry uses. Nothing else does."""
        index = self.round_box.current()
        if index < 0 or index >= len(self.rounds):
            return
        row = self.rounds[index]
        self.play_seq.set(str(row.get("play_seq") or row.get("playSeq") or ""))
        self.play_date.set(self._row_date(row))
        if self._row_time(row):
            self.play_time.set(self._row_time(row))
        self.round_note.set(f"선택: {self._round_label(row)}")
        # The card and the page follow the pick at once: the subtitle used to
        # keep the previous round (2026.09.05 · 회차 026 under a pick of 029),
        # and the overlay kept the page default (001) until an arm was pushed.
        self._refresh_show_where()
        self._refresh_round_line()
        self._publish_round_hint()
        # Only a real click pins the round. `_apply_rounds` calls this too, to
        # seed the box, and that must not count as the user having decided.
        if event is not None:
            self._round_user_picked = True

    @staticmethod
    def _clock_text(hhmm: str) -> str:
        """1930 → 19:30, for the card."""
        digits = re.sub(r"\D", "", str(hhmm or ""))
        return f"{digits[:-2]}:{digits[-2:]}" if len(digits) >= 3 else digits

    def _publish_round_hint(self) -> None:
        """Tell the page which round the panel is aimed at (overlay + catalog)."""
        goods = str(self.goods_code.get() or "").strip()
        place = str(self.place_code.get() or "").strip()
        if not goods:
            return
        try:
            self.browser.publish_show(goods, place, play_seq=self.play_seq.get().strip(),
                                      play_date=self.play_date.get().strip(),
                                      play_time=self.play_time.get().strip())
        except Exception:  # noqa: BLE001 - a hint, never a blocker
            pass

    def _selected_round_label(self) -> str:
        index = self.round_box.current() if hasattr(self, "round_box") else -1
        if 0 <= index < len(self.rounds):
            return self._round_label(self.rounds[index])
        return f"{self._pretty_play_date()} · {self.play_seq.get()}회차"

    def _login_required(self) -> bool:
        """True when the 예매 창 reports no session. None/absent means unknown."""
        context = getattr(self, "_last_context", None) or {}
        return context.get("logged_in") is False

    def enter_now(self) -> None:
        """지금 진입 — enter an already-open show without waiting for anything.

        The scheduled path counts down to 티켓 오픈, so a show that opened
        yesterday could only be armed against a moment already gone, which
        answered 이미 지난 시각입니다 and left no working action at all. This runs
        the same two entry calls immediately.
        """
        if self._login_required():
            self._flash(AMBER, "로그인 필요 — 세션이 없습니다", "예매 창에서 로그인한 뒤 다시 누르세요.")
            return
        try:
            payload = self._arm_payload(target_unix=time.time(), offset_seconds=0.0, dry_run=False)
        except Exception as exc:
            self._note(f"오류: {exc}", error=True)
            return
        self._start_worker(lambda: self._enter_now_worker(payload))

    def _enter_now_worker(self, payload: ArmPayload) -> None:
        try:
            self._ui(self.status.set, "지금 진입…")
            if self._park_for_entry(payload.goods_code):
                self._wait_for_entry_origin(PARK_SETTLE_SECONDS)
            # enabled=False so publishing the arm cannot also start a scheduled
            # run beside this one — the command below is the only thing firing.
            arm = {**payload.to_mapping(), "enabled": False, "fired": False}
            self.browser.push(arm=arm, reload_autopilot=False, command="enter_now")
            self._ui(self.status.set, "지금 진입 요청 보냄")
            self._ui(self._note, "대기열 진입을 시도했습니다 — 예매 창을 확인하세요.")
        except Exception as exc:
            self._ui(self.status.set, str(exc))
            self._ui(self._note, f"오류: {exc}", error=True)

    def _wait_for_entry_origin(self, budget_seconds: float) -> bool:
        """Block until the 예매 창 reports the entry origin, or the budget runs out.

        Polls the context the bridge already writes rather than sleeping a fixed
        amount: a fast park should not cost the same as a slow one.
        """
        deadline = time.perf_counter() + max(0.0, budget_seconds)
        while time.perf_counter() < deadline:
            context = self.browser.read_page_context() or {}
            if not needs_parking(str(context.get("url") or "")):
                return True
            time.sleep(0.2)
        return False

    def _park_for_entry(self, goods_code: str) -> bool:
        """Move the 예매 창 onto the origin the entry calls need, if it is not there.

        The credential the fire spends is minted from
        tickets.interpark.com/api/ticket/v2/reserve-gate/member-info, and that
        call is 401 from nol.yanolja.com — the browser sends no .interpark.com
        cookie on a cross-site request, so the session simply is not there.
        Measured, both origins, on a live login.

        Returns whether it moved, so the caller can give the page time to land
        before arming against it.
        """
        context = self.browser.read_page_context() or {}
        current = str(context.get("url") or "")
        if not needs_parking(current):
            return False
        self.browser.navigate(park_url(goods_code))
        self._ui(self.status.set, "예매 창을 예매 출처로 이동…")
        return True

    def _publish_arm(self, payload: ArmPayload) -> None:
        # What the countdown should be counting to. The clock beside it used to
        # read 티켓 오픈 only, so arming a rehearsal a minute out showed nothing
        # at all — the one minute you most want a clock for. Counting to the
        # moment that will actually fire is the only reading that cannot
        # disagree with what the macro does.
        # The corrected moment, not 티켓 오픈. Counting to the published open
        # while the macro aims 250ms earlier is two clocks disagreeing on the
        # one screen you watch during a race.
        self._armed_target_unix = float(payload.target_server_unix) + payload.entry_offset_ms / 1000
        self._armed_is_test = bool(payload.dry_run) or self._arming_test
        self._push_seat_config(reload_autopilot=True)
        self.browser.push(arm=payload.to_mapping(), reload_autopilot=True, command="run_entry")

    def _arm_worker(self, *, dry_run: bool, target_text: str | None = None, test: bool = False) -> None:
        self._arming_test = test or dry_run
        try:
            # Read the target first. Syncing takes ~2s, and spending it before
            # discovering the time field is empty is how a bad press looked like
            # a dead button rather than a mistake.
            wanted = target_text or self._target_time_text()
            self._ui(self.status.set, "시각 동기화…")
            result = self._sync_now()
            target_unix = parse_target_time(
                wanted,
                self.clock.server_time_unix(),
                target_tz=KST,
            )
            deadline_perf = self.clock.deadline_for_server_time(target_unix)
            if deadline_perf < time.perf_counter() - 0.100:
                raise NolSniperError("이미 지난 시각입니다")
            rtt_ms = float(getattr(result, "best_rtt_seconds", 0.0) or 0.0) * 1000
            auto_lead = default_entry_offset_ms(rtt_ms)
            payload = self._arm_payload(
                target_unix=target_unix,
                offset_seconds=result.offset_seconds,
                dry_run=dry_run,
                auto_offset_ms=auto_lead,
            )
            if payload.entry_offset_ms == auto_lead and self._entry_offset_ms() == 0:
                self._ui(self._note, f"진입 보정 자동 {auto_lead}ms (왕복 {rtt_ms:.0f}ms 기준)")
            # Park before publishing, never after: the arm is what the page acts
            # on, and pushing it at a page that is about to be navigated away
            # from arms the document that is leaving.
            if self._park_for_entry(payload.goods_code):
                # Long enough for the new document to boot the autopilot, capped
                # so a park requested seconds before the open cannot eat it.
                budget = max(0.0, deadline_perf - time.perf_counter() - 0.5)
                self._wait_for_entry_origin(min(PARK_SETTLE_SECONDS, budget))
            self._publish_arm(payload)
            remaining = max(0.0, deadline_perf - time.perf_counter())
            label = "테스트 예약" if test or dry_run else "정시 예약"
            self._ui(self.status.set, f"{label} · {remaining:.1f}초")
            self._ui(self._note, "대기열 진입 예약 · 좌석맵 도착 시 자동 선점"
                if self.auto_start_on.get()
                else "대기열 진입 예약")
        except Exception as exc:
            self._ui(self.status.set, str(exc))
            self._ui(self._note, f"오류: {exc}", error=True)

    def _entry_offset_ms(self) -> int:
        """The 진입 보정 field, clamped, never raising.

        Read on the way into a race, where refusing to arm over a stray
        character is worse than ignoring it. The preview line beside the field
        is what tells you the value did not take.
        """
        return clamp_entry_offset_ms(self.entry_offset_ms.get())

    def _refresh_fire_preview(self) -> None:
        """티켓 오픈 and the moment the correction actually fires at, together.

        Rendered from the same clamp the arm uses, so a value the arm would
        refuse cannot look accepted here.
        """
        raw = self.entry_offset_ms.get().strip()
        offset = self._entry_offset_ms()
        try:
            opens = datetime.strptime(
                f"{self.target_date.get().strip()} {self.target_time.get().strip()}",
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            self.fire_preview.set("")
            return
        if not offset:
            # Say nothing rather than showing the same time twice — the line
            # exists to make a *difference* visible.
            self.fire_preview.set(
                "" if raw in ("", "0", "-0", "+0") else f"'{raw[:8]}'은(는) 숫자가 아닙니다 — 보정 0ms"
            )
            return
        fires = opens + timedelta(milliseconds=offset)
        self.fire_preview.set(
            f"티켓 오픈 {opens:%H:%M:%S}.000 → 실제 발사 {fires:%H:%M:%S}.{fires.microsecond // 1000:03d}"
        )

    def _target_time_text(self) -> str:
        """The 티켓 오픈 fields, in the shape parse_target_time accepts.

        Every failure here used to escape as a raw ValueError — pressing
        대기 시작 with the fields empty reported
        "time data '' does not match format '%Y-%m-%d'", in English, after a
        two-second wait. The panel now enables this button when the open time is
        unknown precisely so it can be typed, which makes an empty field the
        expected case rather than an odd one.
        """
        date_text = self.target_date.get().strip()
        time_text = self.target_time.get().strip()
        if not date_text or not time_text:
            raise NolSniperError("티켓 오픈 날짜와 시각을 입력하세요 (예: 2026-09-05 20:00:00)")
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            raise NolSniperError(f"날짜 형식이 올바르지 않습니다: {date_text} (예: 2026-09-05)") from None
        if len(time_text.split(":")) == 2:
            time_text += ":00"
        parts = time_text.split(":")
        if len(parts) != 3:
            raise NolSniperError(f"시각 형식이 올바르지 않습니다: {time_text} (예: 20:00:00)")
        hour, minute, second = parts
        for part, label in ((hour, "시"), (minute, "분"), (second, "초")):
            if not part.isdigit():
                raise NolSniperError(f"{label}는 숫자여야 합니다")
        return f"{date_text} {int(hour):02d}:{int(minute):02d}:{int(second):02d}.000"

    def _start_browser(self) -> None:
        try:
            self.browser.start(geometry=self.browser_geometry)
            self._push_seat_config(reload_autopilot=True)
            self._note(f"브라우저 · {self.START_URL}")
        except Exception as exc:
            self.status.set(f"브라우저 실패: {exc}")

    # How long to leave between attempts at bringing the 예매 창 back, and how
    # many in a row before giving up and saying so. Relaunching a browser that
    # cannot start, twice a second, forever, is worse than one clear message.
    REOPEN_COOLDOWN_S = 3.0
    REOPEN_MAX_TRIES = 5

    def reopen_browser(self) -> None:
        """Close whatever is left of the 예매 창 and open a fresh one.

        The login is not at risk: on Windows it lives in Chrome's own profile
        directory, and on macOS in the session store — neither is touched here.

        This exists because closing the 예매 창 used to mean quitting and
        relaunching the whole app: the panel kept running against a host that
        was gone, reported 예매 창 응답 없음 forever, and offered nothing to do
        about it.
        """
        self._reopen_tries = 0
        self._note("예매 창을 다시 여는 중…")
        try:
            self.browser.stop()
        except Exception as exc:  # noqa: BLE001 - a corpse that will not die is still replaceable
            self._note(f"이전 예매 창을 정리하지 못했습니다: {exc}", error=True)
        self._start_browser()

    def _keep_browser_alive(self) -> None:
        """Bring the 예매 창 back on its own when it goes away.

        Closing the last Chrome window ends the host process, and nothing used
        to notice. Polled from `_poll_show`, which already runs every 500ms.
        """
        if self.browser.running:
            self._reopen_tries = 0
            return
        now = time.monotonic()
        if now - getattr(self, "_reopen_at", 0.0) < self.REOPEN_COOLDOWN_S:
            return
        self._reopen_at = now
        tries = getattr(self, "_reopen_tries", 0)
        if tries >= self.REOPEN_MAX_TRIES:
            # Say it once, then stop trying. The button is still there.
            if tries == self.REOPEN_MAX_TRIES:
                self._reopen_tries = tries + 1
                self._note("예매 창을 다시 열지 못했습니다 — [예매 창 다시 열기]를 눌러 주세요.",
                           error=True)
            return
        self._reopen_tries = tries + 1
        self._note("예매 창이 닫혔습니다 — 다시 여는 중…")
        self._start_browser()

    def _on_close(self) -> None:
        self.browser.stop()
        self.destroy()

    def _tick_server_time(self) -> None:
        """The clock, and the countdown under it.

        This rescheduled itself with no try/except while _poll_show, right
        beside it, has one. datetime.fromtimestamp raises on an absurd value, so
        a single bad reading would have stopped the clock *and* the countdown
        for the rest of the session — silently, on the most visible element in
        the app.
        """
        try:
            self._render_server_time()
        except Exception as exc:  # noqa: BLE001 - a tick must outlive one bad value
            self.server_time.set("--:--:--")
            self.clock_info.set(f"시각 표시 오류: {exc}")
        finally:
            self.after(100, self._tick_server_time)

    def _render_server_time(self) -> None:
        # A clock held across a sleep is not a clock. perf_counter is
        # mach_absolute_time() and stops while the Mac sleeps, so a panel left
        # open overnight kept counting down to an open that had already passed —
        # while the note beside it, which reads datetime.now(), correctly said
        # 판매 중. Re-sync once rather than rendering either of them as truth.
        #
        # This catches a manually changed device clock as well as a sleep — the
        # test is wall-clock against monotonic, and both move them apart. Worth
        # naming separately, because someone who has just shifted their clock to
        # make a 예매하기 button appear needs to be told which of the two
        # readings in front of them the app is going to believe.
        synced = self.clock.sync_result
        drift = synced.anchor_drift_seconds if synced else 0.0
        if self.clock.anchor_is_stale() and not self._resyncing:
            self.clock_info.set(
                "기기 시계가 바뀌었습니다 — 서버 시각 다시 맞추는 중…"
                if abs(drift) < 600
                else "기기가 절전에서 깨어났습니다 — 서버 시각 다시 맞추는 중…"
            )
            self._resync_now()

        result = self.clock.sync_result
        if result is None:
            self.server_time.set("--:--:--")
            self.clock_info.set("서버 시각 동기화 중…")
        else:
            server_time = datetime.fromtimestamp(self.clock.server_time_unix(), KST)
            millis = server_time.microsecond // 1000
            self.server_time.set(server_time.strftime("%H:%M:%S") + f".{millis:03d}")
            self.clock_info.set(
                f"서버 시각 · 보정 {result.offset_seconds * 1000:+.0f}ms · 정확도 ±{result.best_rtt_seconds * 500:.0f}ms"
            )
        self._tick_countdown()

    def _tick_countdown(self) -> None:
        """Time left until the next thing that will actually fire.

        Uses the same corrected clock the queue entry fires against, so what the
        panel shows and what the macro acts on cannot disagree. The label hides
        itself rather than filling with words when there is nothing to count.

        An armed moment wins over 티켓 오픈. This counted only to the show's own
        open, so arming a rehearsal a minute out — the single most common thing
        anyone does here — showed no clock at all, and the status line's
        "테스트 예약 · 60.0초" was written once and never moved. You were left
        watching a number that had stopped, for the one minute you most wanted a
        clock for.
        """
        armed = self._armed_target_unix
        prefix = ""
        if armed is not None:
            remaining = float(armed) - self.clock.server_time_unix()
            if remaining > 0:
                prefix = "테스트 " if self._armed_is_test else ""
                self._show_countdown(prefix + self._countdown_text(remaining))
                return
            # It has fired, or its moment has passed. Stop claiming it is next.
            self._armed_target_unix = None
            self._armed_is_test = False

        try:
            target = parse_target_time(
                f"{self.target_date.get().strip()} {self.target_time.get().strip()}",
                target_tz=KST,
            )
        except Exception:  # noqa: BLE001 - an unparseable time has nothing to show
            self._show_countdown(None)
            return

        # parse_target_time already returns a unix timestamp. The correction is
        # applied here too, so the number on screen before you arm is the same
        # one you will be counting down after.
        remaining = float(target) + self._entry_offset_ms() / 1000 - self.clock.server_time_unix()
        if remaining <= 0:
            self._show_countdown(None)
            return
        self._show_countdown(self._countdown_text(remaining))

    @staticmethod
    def _countdown_text(remaining: float) -> str:
        # Days matter for a show weeks out: 3033:41:30 is unreadable as a
        # countdown, and the hours field is what people check in the last hour.
        days, rest = divmod(int(remaining), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{days}일 {clock}" if days else clock

    def _show_countdown(self, text: str | None) -> None:
        label = getattr(self, "countdown_label", None)
        if label is None:
            return
        # Never packed or unpacked: the label keeps its line and only the text
        # changes, so nothing below it moves.
        self.countdown.set("" if text is None else text)

    def _ui(self, fn, /, *args, **kwargs) -> None:
        self.after(0, lambda: fn(*args, **kwargs))


if __name__ == "__main__":
    NolSniperApp().mainloop()
