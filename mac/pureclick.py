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
from core.arm import ArmPayload  # noqa: E402
from core.clock import KST, PureClickError, ServerClock, parse_target_time  # noqa: E402
from pureclick_mac_core import ensure_mac_ready  # noqa: E402
from core.seat import (  # noqa: E402
    SeatPreferences,
    parse_goods_code,
    map_move_lines,
    running_hint,
    waiting_log_lines,
    seat_order_lines,
    serialize_preferences,
)
from core.showinfo import seat_table_lines, fetch_round_remains, fetch_show_catalog  # noqa: E402
from core.watch_trigger import TriggerState, next_trigger_state  # noqa: E402
from core.zone_map import (  # noqa: E402
    block_keys_in_watch_rect,
    is_click,
    parse_box,
    parse_watch_rect,
    project_venue,
    seat_pitch,
    seats_in_watch_rect,
)

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

# Apple SD Gothic Neo is the macOS Korean face and renders 한글 far better than
# any Latin-first default; SF Mono gives the clock and countdown real numerals.
UI_FONT = "Apple SD Gothic Neo"
# SF Mono is not exposed to Tk; Menlo is the same lineage and is.
MONO_FONT = "Menlo"
DANGER = "#ef4444"


class PureClickMacApp(tk.Tk):
    # Aiming strategies, in the order they appear in the picker.
    # All three go for the stage first; the label's tail says which side of the
    # house to look at when several seats are equally close.
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
        super().__init__()
        ensure_mac_ready()
        self.title("NOL 스나이퍼 · 조작판")
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
        self.test_time = tk.StringVar(value=soon.strftime("%H:%M:%S"))
        self.test_result = tk.StringVar(value="")
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
        self.zone_summary = tk.StringVar(value="감시 구역: 전체")
        self.clock_info = tk.StringVar(value="서버 시각 동기화 중…")
        self.guidance = tk.StringVar(
            value="지금 할 일 — 다른 창(NOL 예매)에서 공연을 클릭하세요. 조작판이 자동으로 채워집니다."
        )
        self.btn_arm = None
        self.btn_catch = None
        self.status = tk.StringVar(value="준비")
        self.reason = tk.StringVar(value="")
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
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _plan_layout(self) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Split the screen: control panel on the left, 예매 창 on the right.

        Returns (x, y, width, height) for each. The seat map needs the wider
        half — it is the thing being read during a race — so the panel takes a
        third and never less than a readable 460.
        """
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        top = 28  # under the menu bar
        height = max(640, screen_h - top - 40)

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
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(holder, width=e.width))

        # Scoped to the pointer, so the 오픈 예정 목록 window keeps its own wheel.
        def wheel(event: tk.Event) -> None:
            canvas.yview_scroll(-1 * int(event.delta), "units")

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
            tk.Label(head, textvariable=note, bg=PANEL, fg=MUTED, anchor="w",
                     wraplength=self.panel_geometry[2] - 110, justify="left",
                     font=(UI_FONT, 11)).pack(anchor="w", pady=(3, 0))
        body = tk.Frame(shell, bg=PANEL)
        body.pack(fill="x", padx=18, pady=(12, 18))
        return body

    def _build_ui(self) -> None:
        """Two functions, both always visible, and a clock to run them against.

        Headings are dim and small; the accent belongs to the buttons and the
        live dot and nothing else. Colour used for emphasis everywhere is what
        made this read as an instrument panel rather than a tool.
        """
        root = self._scrollable_root()
        wrap = self.panel_geometry[2] - 76

        # --- Masthead ---------------------------------------------------------
        head = ttk.Frame(root)
        head.pack(fill="x")
        ttk.Label(head, text="스나이퍼", style="Wordmark.TLabel").pack(side="left")
        ttk.Label(head, textvariable=self.server_time, style="Clock.TLabel").pack(side="right")
        ttk.Label(root, textvariable=self.clock_info, style="Faint.TLabel").pack(anchor="e", pady=(1, 0))

        # --- What we are aiming at -------------------------------------------
        show = self._card(root, "공연")
        ttk.Label(show, textvariable=self.show_title, style="CardTitle.TLabel",
                  wraplength=wrap - 40, justify="left").pack(anchor="w")
        ttk.Label(show, textvariable=self.show_where, style="CardMuted.TLabel",
                  wraplength=wrap - 40, justify="left").pack(anchor="w", pady=(3, 0))
        # The 회차 belongs on screen: it changes underneath everything else and
        # every seat the macro targets is keyed to it.
        ttk.Label(show, textvariable=self.show_round, style="CardFaint.TLabel",
                  wraplength=wrap - 40, justify="left").pack(anchor="w", pady=(2, 0))
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
        tip_edge = tk.Frame(root, bg=BORDER)
        tip_edge.pack(fill="x", pady=(16, 0))
        tip = tk.Frame(tip_edge, bg=PANEL_2)
        tip.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(tip, textvariable=self.guidance, bg=PANEL_2, fg=FG, anchor="w",
                 font=(UI_FONT, 12), wraplength=wrap - 24,
                 justify="left").pack(fill="x", padx=14, pady=12)

        # --- How it chooses ---------------------------------------------------
        aim = self._card(root, "좌석 고르는 순서")
        pick = ttk.Frame(aim, style="Card.TFrame")
        pick.pack(fill="x")
        pick.columnconfigure(0, weight=1)
        self.strategy_box = ttk.Combobox(pick, values=list(self.STRATEGY_LABELS.values()),
                                         state="readonly")
        self.strategy_box.grid(row=0, column=0, sticky="ew")
        self.strategy_box.set(
            self.STRATEGY_LABELS.get(self.seat_strategy.get(), self.STRATEGY_LABELS["center"])
        )
        self.strategy_box.bind("<<ComboboxSelected>>", self._on_strategy_pick)
        ttk.Label(pick, text="매수", style="CardFaint.TLabel").grid(row=0, column=1, padx=(12, 6))
        ttk.Combobox(pick, textvariable=self.quantity, values=["1", "2", "3", "4"],
                     state="readonly", width=3).grid(row=0, column=2)
        # Why it chose what it chose. Empty until the first attempt ranks
        # anything, so it costs nothing on screen before then.
        self.order_text = tk.Text(aim, height=1, bg=PANEL_2, fg=MUTED, insertbackground=FG,
                                  highlightthickness=0, borderwidth=0, wrap="none",
                                  font=(MONO_FONT, 10), spacing1=1)
        self.order_text.configure(state="disabled")

        # --- Open 대기 ---------------------------------------------------------
        openq = self._card(root, "오픈 대기", self.open_note)
        when = tk.Frame(openq, bg=PANEL)
        when.pack(fill="x")
        tk.Label(when, text="티켓 오픈", bg=PANEL, fg=MUTED, font=(UI_FONT, 11)).pack(side="left", padx=(0, 8))
        ttk.Entry(when, textvariable=self.target_date, width=11).pack(side="left")
        ttk.Entry(when, textvariable=self.target_time, width=9).pack(side="left", padx=(6, 0))

        self.countdown_label = ttk.Label(openq, textvariable=self.countdown, style="CardHero.TLabel")
        self.btn_arm = ttk.Button(openq, text="대기 시작", style="Primary.TButton", command=self.arm)
        self.btn_arm.pack(fill="x", pady=(12, 0))
        self.btn_arm_stop = ttk.Button(openq, text="대기 중지", style="CardGhost.TButton",
                                       command=self.stop_arm)
        self.btn_arm_stop.pack(fill="x", pady=(4, 0))
        tk.Checkbutton(openq, text="들어가면 곧바로 좌석까지 잡기", variable=self.auto_start_on,
                       command=self._push_seat_config, bg=PANEL, fg=FG, selectcolor=PANEL_2,
                       activebackground=PANEL, activeforeground=FG, highlightthickness=0,
                       font=(UI_FONT, 12), anchor="w").pack(anchor="w", pady=(12, 0))
        tk.Label(openq, text="보안문자만 직접 입력하면 됩니다.", bg=PANEL, fg=FAINT,
                 font=(UI_FONT, 11), anchor="w").pack(anchor="w", pady=(2, 0))

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
        tk.Label(openq, textvariable=self.test_result, bg=PANEL, fg=GREEN, anchor="w",
                 justify="left", font=(MONO_FONT, 11)).pack(anchor="w", pady=(8, 0))

        # --- 취켓팅 -------------------------------------------------------------
        catch = self._card(root, "취켓팅", self.catch_note)
        zone = tk.Frame(catch, bg=PANEL)
        zone.pack(fill="x")
        zone.columnconfigure(0, weight=1)
        tk.Label(zone, textvariable=self.zone_summary, bg=PANEL, fg=FG, anchor="w",
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

        # --- Live --------------------------------------------------------------
        live_edge = tk.Frame(root, bg=BORDER)
        live_edge.pack(fill="x", pady=(20, 0))
        live = tk.Frame(live_edge, bg=PANEL_2)
        live.pack(fill="both", expand=True, padx=1, pady=1)
        self.status_dot = tk.Label(live, text="●", bg=PANEL_2, fg=FAINT, font=(UI_FONT, 11))
        self.status_dot.pack(anchor="w", padx=16, pady=(14, 0))
        tk.Label(live, textvariable=self.status, bg=PANEL_2, fg=FG, anchor="w",
                 font=(UI_FONT, 13, "bold"), wraplength=wrap - 20,
                 justify="left").pack(fill="x", padx=16)
        tk.Label(live, textvariable=self.reason, bg=PANEL_2, fg=MUTED, anchor="w",
                 font=(UI_FONT, 11), wraplength=wrap - 20,
                 justify="left").pack(fill="x", padx=16, pady=(4, 14))
        ttk.Button(root, text="전부 정지", style="Ghost.TButton", command=self.stop_all).pack(
            fill="x", pady=(10, 30)
        )

        self._update_guidance(None)



    def stop_all(self) -> None:
        try:
            self._stop_trigger_worker()
            self.browser.send_command("stop_all", clear_arm=True)
            self.status.set("정지 요청됨")
        except Exception as exc:
            self.status.set(f"정지 실패: {exc}")

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
        config_path = MAC_DIR / "pureclick_seat_config.json"
        config_path.write_text(serialize_preferences(preferences), encoding="utf-8")
        self.browser.push(
            seat=preferences.to_mapping(),
            reload_autopilot=reload_autopilot,
            command=command,
            clear_arm=clear_arm,
        )

    def _load_seat_config(self) -> None:
        path = MAC_DIR / "pureclick_seat_config.json"
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
            self.seat_strategy.set(
                self.LEGACY_STRATEGIES.get(preferences.seat_strategy, preferences.seat_strategy)
            )
            self.auto_assign_on.set(preferences.auto_assign)
            self.reentry_on.set(preferences.reentry)
            self.auto_start_on.set(preferences.auto_seats_after_entry)
        except Exception:
            pass

    def _sync_now_bg(self) -> None:
        self._start_worker(self._sync_worker)


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
        # The API only knows the run's first date; if the browser is showing a
        # particular date, that is the one being booked.
        if info.get("play_start_date") and not self.play_date.get().strip():
            self.play_date.set(str(info["play_start_date"]))
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
        warnings = list(info.get("warnings") or []) + list(info.get("errors") or [])
        if info.get("flow") == "legacy-poticket":
            self.status.set("이 공연은 구형 엔진이라 자동 선점을 지원하지 않습니다")
        else:
            self.status.set(f"공연 불러옴 · 등급 {len(self._grade_rows)}개")
        if warnings:
            self._note(" · ".join(warnings), error=True)
        else:
            self._note(f"상품 {info.get('goods_code')} · 회차 {self.play_seq.get()}")
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
        time_text = self.play_time.get().strip()
        round_text = " ".join(part for part in (pretty, time_text) if part)
        self.show_where.set(" · ".join(part for part in (round_text, place_text) if part) or place_text)

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
            self.status.set("대기 취소됨")
            self._note("오픈 대기를 해제했습니다")
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            self.status.set(f"오류: {exc}")

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

        window = tk.Toplevel(self)
        window.title("감시 구역")
        window.geometry(self._zone_window_size())
        window.configure(bg=BG)
        self._zones = window

        ttk.Label(
            window,
            text="드래그해서 감시할 범위를 정하세요. 그 안에서 나오는 자리는 등급과 무관하게 잡습니다.",
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
        if not self._zone_sketch:
            return "좌석맵에 들어가면 지도가 채워집니다."
        if self._watch_rect is None:
            return f"{len(self._zone_sketch)}석 · 범위를 지정하지 않으면 전체를 감시합니다."
        inside = seats_in_watch_rect(self._zone_sketch, self._rect_tuple())
        return f"{len(self._zone_sketch)}석 중 {len(inside)}석 감시 중"

    def _rect_tuple(self) -> tuple[float, float, float, float] | None:
        rect = self._watch_rect
        if not rect:
            return None
        return (rect["left"], rect["top"], rect["right"], rect["bottom"])

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
            canvas.create_text(
                width / 2,
                height / 2,
                text="예매 창에서 좌석맵에 들어가면\n여기에 좌석 배치가 그려집니다",
                fill=MUTED,
                justify="center",
            )
            self._zone_view = None
            return

        view = project_venue(
            [{"block_key": row["key"], **row} for row in self._block_rows],
            self._zone_sketch,
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
            inside = rect is None or (
                rect[0] <= seat.venue_x <= rect[2] and rect[1] <= seat.venue_y <= rect[3]
            )
            colour = ACCENT if inside else "#3b4a42"
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
            inside = seats_in_watch_rect(self._zone_sketch, self._rect_tuple())
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
        # Always follow the 예매판 round — these are what the user is looking at.
        if catalog.get("play_date"):
            self.play_date.set(str(catalog["play_date"]))
        if catalog.get("play_seq"):
            self.play_seq.set(str(catalog["play_seq"]))
        if catalog.get("play_time"):
            self.play_time.set(str(catalog["play_time"]))
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

        if incoming:
            self._apply_blocks(incoming)
            sketch = catalog.get("sketch")
            if isinstance(sketch, list) and sketch:
                self._zone_sketch = [row for row in sketch if isinstance(row, dict)]
            self._schedule_zone_map()
        elif target_changed:
            self._watch_rect = None
            self._apply_blocks([])
        else:
            sketch = catalog.get("sketch")
            if isinstance(sketch, list) and sketch:
                self._zone_sketch = [row for row in sketch if isinstance(row, dict)]
                self._schedule_zone_map()
            self._refresh_zone_picker()

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
            self._push_seat_config(command="run_catch", clear_arm=True)
            self.status.set("취켓팅 감시 중…")
            self._note("seatStatus 변화 감시")
            self._trigger_state = None
            self._trigger_on = True
            self._start_trigger_worker()
        except Exception as exc:
            self.status.set(f"오류: {exc}")

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
            except Exception:
                pass
            time.sleep(self.TRIGGER_POLL_MS / 1000)

    def _stop_trigger_worker(self) -> None:
        self._trigger_on = False
        self._trigger_state = None

    def arm(self, *, dry_run: bool = False) -> None:
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
        """
        try:
            when = datetime.strptime(self._test_time_text(), "%Y-%m-%d %H:%M:%S")
        except (ValueError, PureClickError):
            when = datetime.now(KST).replace(tzinfo=None)
        self._set_test_time(when + timedelta(minutes=1))

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
                raise PureClickError(f"{label}는 숫자여야 합니다")
            parts.append(int(raw))
        hour, minute, second = parts
        if hour > 23 or minute > 59 or second > 59:
            raise PureClickError("시각이 올바르지 않습니다")
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
        return self.clock.sync_tick(
            self.SYNC_URL,
            sample_count=self.SYNC_SAMPLES,
            min_samples=2,
            max_wait_seconds=8.0,
            poll_seconds=0.005,
        )

    def _poll_show(self) -> None:
        try:
            context = self.browser.read_page_context()
            if context:
                self._follow_browser_show(context)
            self._update_guidance(context)

            catalog = self.browser.read_show_catalog()
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
                if (
                    catalog.get("fetched_at") != prev.get("fetched_at")
                    or len(new_blocks) != len(old_blocks)
                    or len(new_sketch) != len(old_sketch)
                    or grade_sig != prev_grade_sig
                    or round_changed
                ):
                    self._apply_catalog(catalog)
            self._apply_autopilot_status()
        except Exception as exc:  # noqa: BLE001 - keep the poll alive
            self._note(f"브라우저 동기화 오류: {exc}", error=True)
        self.after(500, self._poll_show)

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
        if not arm.get("fired") and not str(arm.get("lastError") or "").strip():
            return

        lateness = arm.get("latenessMs")
        lines = []
        error = str(arm.get("lastError") or "").strip()
        via = {
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
            lines.append(row)

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

        host = str(arm.get("queueHost") or "").strip()
        if host:
            lines.append(f"대기열 호스트 {host} · 다음 진입부터 미리 연결")

        # What the queue endpoint actually said either side of the open. This is
        # the record that decides whether polling across the boundary is even
        # the right method.
        lines.extend(waiting_log_lines(arm))

        self.test_result.set("\n".join(lines))

    def _apply_autopilot_status(self) -> None:
        """Mirror what the 예매 창 is doing, with the numbers that explain it.

        The status line used to be the message cut at its first ' · ', which
        threw away the part that said *why* — leaving '취소표 감시 중' on screen
        whether it was watching an empty map, ignoring seats that did not match
        the chosen grades, or losing every race.
        """
        status = self.browser.read_autopilot_status() or {}
        self._render_entry_result(status.get("arm") or {})
        seat = status.get("seat") or {}
        message = str(seat.get("message") or "").strip()
        if not message:
            return

        attempts = seat.get("attempts") or 0

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

        self._render_seat_order(seat)

        # The dot carries the state so the text does not have to spell it out
        # with symbols — one glance tells you whether anything is happening.
        if seat.get("locked"):
            self._set_state(GREEN, f"좌석 잡음 · {seat.get('lastSeat') or ''}".strip(" ·"),
                            "예매 창에서 결제만 진행하세요. 결제 버튼은 누르지 않습니다.")
        elif seat.get("running"):
            head = message.split(" · ")[0]
            self._set_state(ACCENT, f"{head} · 시도 {attempts}회" if attempts else head,
                            running_hint(seat, message))
        elif seat.get("haltedByUser"):
            self._set_state(FAINT, f"멈춤 · 시도 {attempts}회", "다시 하려면 위 버튼을 누르세요.")
        elif seat.get("lastError"):
            self._set_state(AMBER, "중단됨", str(seat.get("lastError")))
        else:
            self._set_state(FAINT, f"대기 중 · 시도 {attempts}회" if attempts else "대기 중", "")

    def _render_seat_order(self, seat: dict) -> None:
        """Show the ranking, and take the space back when there is none."""
        box = getattr(self, "order_text", None)
        if box is None or not box.winfo_exists():
            return
        # The ordering, and underneath it what reaching those seats cost. Both
        # answer the same question — why did it take that seat, and why then.
        lines = seat_order_lines(seat)
        moves = map_move_lines(seat)
        if moves:
            lines = [*lines, *([""] if lines else []), *moves]
        if not lines:
            box.pack_forget()
            return
        box.configure(state="normal", height=len(lines))
        box.delete("1.0", tk.END)
        box.insert("1.0", "\n".join(lines))
        box.configure(state="disabled")
        if not box.winfo_manager():
            box.pack(fill="x", pady=(10, 0))

        self._render_seat_order(seat)

        # The dot carries the state so the text does not have to spell it out
        """A one-off message the user needs to see.

        These all used to go to `log_text` — a StringVar bound to no widget. Two
        dozen call sites wrote to it, including every error path in the arm
        worker, the browser sync and the remain refresh, so a failed 대기 시작
        reported itself somewhere nobody could read. They land on the live line
        now, and errors take the dot amber with them.
        """
        if error:
            self._set_state(AMBER, "문제가 발생했습니다", text)
        else:
            self.reason.set(text)

    def _set_state(self, colour: str, headline: str, why: str) -> None:
        self.status.set(headline)
        self.reason.set(why)
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
            self.goods_code.set(code)
            self.product_url.set(f"https://nol.yanolja.com/ticket/products/{code}")
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
            self._schedule_remain_refresh(context)

    def _refresh_round_line(self) -> None:
        """The 일정 the macro is targeting, on screen.

        It changes underneath everything else — every block key the macro uses
        embeds it — and until now nothing showed it, so a panel aimed at the
        wrong round looked identical to one aimed at the right one.
        """
        date = self._pretty_play_date()
        parts = [part for part in (date, self.play_time.get().strip()) if part]
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

    def _update_guidance(self, context: dict | None) -> None:
        """Say what to do next, and only enable the buttons that can work.

        Which step you are on is decided by the page the browser is actually
        showing, so the panel cannot disagree with the window next to it.
        """
        page = (context or {}).get("page") or ""
        loaded = self._show_info_data
        on_seat_map = page == "seat"
        goods_on_page = self._browser_goods_code(context)

        if not loaded:
            if goods_on_page:
                step, hint = 1, f"예매 창에서 {goods_on_page}를 감지했습니다. 공연 정보를 가져오는 중…"
            else:
                step, hint = 1, "다른 창(NOL 예매)에서 공연을 클릭하세요. 조작판이 자동으로 채워집니다."
        elif loaded.get("flow") == "legacy-poticket":
            step, hint = 0, "이 공연은 구형 예매 엔진이라 자동화를 지원하지 않습니다."
        elif page in {"waiting", "gates"}:
            step, hint = 3, "대기열 진입 중입니다. 그대로 기다리세요."
        elif on_seat_map:
            step, hint = 4, "좌석맵 도착 — [감시 시작]을 누르면 고른 범위를 지켜봅니다."
        elif self._sale_open():
            step, hint = 3, "예매 창에서 로그인 후 [예매하기]를 눌러 좌석맵으로 이동하세요."
        elif self._open_time() is None:
            step, hint = 2, "티켓 오픈 시각을 알 수 없습니다 — 위에 직접 입력하고 [대기 시작]을 누르세요."
        else:
            step, hint = 2, "판매 전입니다. 예매 창에서 로그인해 두고 [대기 시작]을 누르세요."

        self.guidance.set(f"지금 할 일 — {hint}" if step else f"안내 — {hint}")

        self._set_enabled(self.btn_arm, step == 2)
        # The rehearsal arms the real scheduler, so it belongs on the same pages
        # a real entry does. It had no reference kept, so nothing could disable
        # it: pressed on the seat map it armed a scheduler with nothing to enter
        # and reported nothing at all.
        can_enter = page in {"nol", "goods"}
        self._set_enabled(getattr(self, "btn_test", None), can_enter)
        self._set_enabled(self.btn_catch, on_seat_map)
        # Say when a function cannot apply, rather than hiding it. Both stay on
        # screen; only the explanation changes.
        if self._show_info_data and self._sale_open():
            self.open_note.set("판매 중 — 이미 열린 공연이라 대기가 필요 없습니다.")
        else:
            self.open_note.set("아직 안 열린 공연 — 열리는 순간 대기열을 먼저 잡습니다.")


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
            raise PureClickError("상품코드가 없습니다. 공연 페이지를 열고 정보를 가져오세요")
        try:
            return parse_goods_code(raw)
        except Exception:
            return raw.upper()

    def _arm_payload(self, *, target_unix: float, offset_seconds: float, dry_run: bool) -> ArmPayload:
        play_date = self.play_date.get().strip().replace("-", "")
        play_seq = self.play_seq.get().strip() or "001"
        goods_code = self._resolved_goods()
        if not play_date.isdigit() or len(play_date) != 8:
            raise PureClickError("공연일은 YYYYMMDD 형식이어야 합니다")
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
        )

    def _publish_arm(self, payload: ArmPayload) -> None:
        self._push_seat_config(reload_autopilot=True)
        self.browser.push(arm=payload.to_mapping(), reload_autopilot=True, command="run_entry")

    def _arm_worker(self, *, dry_run: bool, target_text: str | None = None, test: bool = False) -> None:
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
                raise PureClickError("이미 지난 시각입니다")
            payload = self._arm_payload(
                target_unix=target_unix,
                offset_seconds=result.offset_seconds,
                dry_run=dry_run,
            )
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
            raise PureClickError("티켓 오픈 날짜와 시각을 입력하세요 (예: 2026-09-05 20:00:00)")
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            raise PureClickError(f"날짜 형식이 올바르지 않습니다: {date_text} (예: 2026-09-05)") from None
        if len(time_text.split(":")) == 2:
            time_text += ":00"
        parts = time_text.split(":")
        if len(parts) != 3:
            raise PureClickError(f"시각 형식이 올바르지 않습니다: {time_text} (예: 20:00:00)")
        hour, minute, second = parts
        for part, label in ((hour, "시"), (minute, "분"), (second, "초")):
            if not part.isdigit():
                raise PureClickError(f"{label}는 숫자여야 합니다")
        return f"{date_text} {int(hour):02d}:{int(minute):02d}:{int(second):02d}.000"

    def _start_browser(self) -> None:
        try:
            self.browser.start(geometry=self.browser_geometry)
            self._push_seat_config(reload_autopilot=True)
            self._note(f"브라우저 · {self.START_URL}")
        except Exception as exc:
            self.status.set(f"브라우저 실패: {exc}")

    def _on_close(self) -> None:
        self.browser.stop()
        self.destroy()

    def _tick_server_time(self) -> None:
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
        self.after(100, self._tick_server_time)

    def _tick_countdown(self) -> None:
        """Time left until the show opens, on the server's clock.

        Uses the same corrected clock the queue entry fires against, so what the
        panel shows and what the macro acts on cannot disagree. The label hides
        itself rather than filling with words when there is nothing to count.
        """
        try:
            target = parse_target_time(
                f"{self.target_date.get().strip()} {self.target_time.get().strip()}",
                target_tz=KST,
            )
        except Exception:  # noqa: BLE001 - an unparseable time has nothing to show
            self._show_countdown(None)
            return

        # parse_target_time already returns a unix timestamp.
        remaining = float(target) - self.clock.server_time_unix()
        if remaining <= 0:
            self._show_countdown(None)
            return
        # Days matter for a show weeks out: 3033:41:30 is unreadable as a
        # countdown, and the hours field is what people check in the last hour.
        days, rest = divmod(int(remaining), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self._show_countdown(f"{days}일 {clock}" if days else clock)

    def _show_countdown(self, text: str | None) -> None:
        label = getattr(self, "countdown_label", None)
        if label is None:
            return
        if text is None:
            if label.winfo_manager():
                label.pack_forget()
            self.countdown.set("")
            return
        self.countdown.set(text)
        if not label.winfo_manager():
            label.pack(anchor="w", pady=(14, 0), before=self.btn_arm)

    def _ui(self, fn, /, *args, **kwargs) -> None:
        self.after(0, lambda: fn(*args, **kwargs))


if __name__ == "__main__":
    PureClickMacApp().mainloop()
