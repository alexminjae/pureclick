"""The platform seam, and the Windows half of it exercised on a Mac.

None of the Windows code can be run against a real WebView2 from here, so what
is testable is its *logic*, with the native calls faked. That is worth doing
because the two things most likely to go wrong are logic, not API:

  * WebView2 has no `removeAllUserScripts()`. It returns a script id, and the
    previous one has to be removed by id — otherwise every `reload_autopilot`
    stacks another copy of a 325 KB script that runs on every document creation.
  * The CoreWebView2 object is null until initialisation finishes, exactly as
    the Cocoa `.webview` is, so both need the same wait.

The last test here is the one that keeps Windows working after I stop looking:
it fails if any shared module imports a platform-only library again.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "mac") not in sys.path:
    sys.path.insert(0, str(ROOT / "mac"))

import app_platform  # noqa: E402
from app_platform import darwin, windows  # noqa: E402


# ---- fakes standing in for pythonnet and WebView2 -------------------------

class _FakeTask:
    def __init__(self, result):
        self.Result = result

    def ContinueWith(self, action, _scheduler):
        action(self)          # the real scheduler runs it on the UI thread
        return self


class _Generic:
    """Stands in for `Func[Object]` / `Action[Task[Object]]` — identity."""

    def __class_getitem__(cls, _item):
        return lambda fn: fn


class _FakeTaskScheduler:
    Default = object()


def _fake_clr():
    return _Generic, _Generic, object, str, _Generic, _FakeTaskScheduler


class _FakeCore:
    def __init__(self):
        self.added: list[str] = []
        self.removed: list[str] = []
        self._next = 0
        self.CookieManager = _FakeCookieManager()

    def AddScriptToExecuteOnDocumentCreatedAsync(self, source):
        self._next += 1
        script_id = f"id-{self._next}"
        self.added.append(script_id)
        assert source, "an empty script would silently disable the macro"
        return _FakeTask(script_id)

    def RemoveScriptToExecuteOnDocumentCreated(self, script_id):
        self.removed.append(script_id)


class _FakeCookie:
    def __init__(self, name, value, domain, path):
        self.Name, self.Value, self.Domain, self.Path = name, value, domain, path
        self.Expires, self.IsSecure, self.IsHttpOnly = -1.0, False, False


class _FakeCookieManager:
    def __init__(self):
        self.stored: list[_FakeCookie] = []

    def CreateCookie(self, name, value, domain, path):
        return _FakeCookie(name, value, domain, path)

    def AddOrUpdateCookie(self, cookie):
        self.stored.append(cookie)

    def GetCookiesAsync(self, _url):
        return _FakeTask(list(self.stored))


class _FakeControl:
    def __init__(self, core):
        self.CoreWebView2 = core

    def Invoke(self, fn):
        return fn()


class _FakeBrowser:
    def __init__(self, core):
        self.webview = _FakeControl(core)
        self.syncContextTaskScheduler = object()


class WindowsInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = _FakeCore()
        self.browser = _FakeBrowser(self.core)
        self._clr, self._browser = windows._clr, windows._browser
        self._message_loop_running = windows._message_loop_running
        self._pump_messages = windows._pump_messages
        windows._clr = _fake_clr
        windows._browser = lambda _window: self.browser
        # These tests exercise the background-thread path — the common case,
        # and the one every test here was originally written against. The
        # UI-thread pumping path has its own tests below.
        windows._message_loop_running = lambda: False
        windows._pump_messages = lambda: None
        windows._script_ids.clear()
        self.window = type("W", (), {"uid": "master"})()

    def tearDown(self) -> None:
        windows._clr, windows._browser = self._clr, self._browser
        windows._message_loop_running = self._message_loop_running
        windows._pump_messages = self._pump_messages
        windows._script_ids.clear()

    def test_the_previous_script_is_removed_before_the_new_one(self) -> None:
        windows.install_document_start_script(self.window, "// v1")
        self.assertEqual(self.core.added, ["id-1"])
        self.assertEqual(self.core.removed, [])

        windows.install_document_start_script(self.window, "// v2")
        # Every reload_autopilot goes through here. Without the removal, WebView2
        # keeps running v1 as well, forever.
        self.assertEqual(self.core.removed, ["id-1"], "the old script must be dropped")
        self.assertEqual(self.core.added, ["id-1", "id-2"])

    def test_only_one_script_is_ever_registered(self) -> None:
        for n in range(5):
            windows.install_document_start_script(self.window, f"// v{n}")
        live = set(self.core.added) - set(self.core.removed)
        self.assertEqual(len(live), 1, f"exactly one document-start script, got {live}")

    def test_a_missing_webview_is_an_error_not_a_silent_pass(self) -> None:
        windows._browser = lambda _window: None
        with self.assertRaises(RuntimeError):
            windows.install_document_start_script(self.window, "// v1")

    def test_the_wait_gives_up_rather_than_hanging(self) -> None:
        """A null CoreWebView2 must not spin forever on the caller's thread."""
        import webview.platforms  # noqa: F401  - only to prove the import shape

        windows._browser = self._browser          # the real waiter
        original_tries = windows._WEBVIEW_WAIT_TRIES
        windows._WEBVIEW_WAIT_TRIES = 2
        windows._WEBVIEW_WAIT_SECONDS = 0.001
        try:
            done = threading.Event()
            result: list[object] = []

            def run() -> None:
                try:
                    result.append(windows._browser(self.window))
                except Exception as exc:  # noqa: BLE001
                    result.append(exc)
                done.set()

            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(done.wait(5), "the wait must be bounded")
        finally:
            windows._WEBVIEW_WAIT_TRIES = original_tries


class WindowsCookieTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = _FakeCore()
        self.browser = _FakeBrowser(self.core)
        self._clr, self._browser = windows._clr, windows._browser
        self._message_loop_running = windows._message_loop_running
        self._pump_messages = windows._pump_messages
        windows._clr = _fake_clr
        windows._browser = lambda _window: self.browser
        windows._message_loop_running = lambda: False
        windows._pump_messages = lambda: None
        self.window = type("W", (), {"uid": "master"})()

    def tearDown(self) -> None:
        windows._clr, windows._browser = self._clr, self._browser
        windows._message_loop_running = self._message_loop_running
        windows._pump_messages = self._pump_messages

    def test_a_jar_survives_a_round_trip(self) -> None:
        """This is the difference between logging in once and logging in daily."""
        jar = [
            {"Name": "NID_AUT", "Value": "abc", "Domain": ".naver.com", "Path": "/",
             "Secure": "TRUE", "HttpOnly": True, "Expires": 1788253200.0},
            {"Name": "NID_SES", "Value": "xyz", "Domain": ".naver.com", "Path": "/"},
        ]
        self.assertEqual(windows.restore_cookies(self.window, jar), 2)
        back = {row["Name"]: row for row in windows.dump_cookies(self.window)}

        self.assertEqual(back["NID_AUT"]["Value"], "abc")
        self.assertEqual(back["NID_AUT"]["Domain"], ".naver.com")
        self.assertEqual(back["NID_AUT"]["Secure"], "TRUE")
        self.assertIs(back["NID_AUT"]["HttpOnly"], True)
        self.assertAlmostEqual(back["NID_AUT"]["Expires"], 1788253200.0)
        # A session cookie is recorded by omission, which is what restore expects.
        self.assertNotIn("Expires", back["NID_SES"])

    def test_a_nameless_row_is_skipped_not_stored(self) -> None:
        self.assertEqual(windows.restore_cookies(self.window, [{"Value": "x"}]), 0)


class SameThreadCallTests(unittest.TestCase):
    """Reported live: the panel fine, the whole 예매창 gone 응답 없음.

    pywebview fires Shown/loaded synchronously on the UI thread, and
    install_document_start_script is called directly from both. The original
    _await_on_ui always did Invoke(...) then blocked on a Semaphore waiting for
    a syncContext continuation — safe from a background thread, a real deadlock
    called from the UI thread itself: Invoke runs the delegate inline there, so
    the block that follows blocks the one thread the continuation needs in
    order to ever run, and nothing pumps Windows messages while it sits there.

    A test that merely doesn't hang could pass by accident if the fake
    completes the task before the wait loop is ever entered — exactly what
    _FakeTask.ContinueWith above does, firing synchronously. So the fakes here
    are deliberately different: the task's completion is withheld until
    _pump_messages has actually been called some number of times, simulating a
    native completion delivered through the message loop. A test that never
    proves pumping happened is not testing the fix.
    """

    def setUp(self) -> None:
        self._clr = windows._clr
        self._message_loop_running = windows._message_loop_running
        self._pump_messages = windows._pump_messages
        windows._clr = _fake_clr

    def tearDown(self) -> None:
        windows._clr = self._clr
        windows._message_loop_running = self._message_loop_running
        windows._pump_messages = self._pump_messages

    def test_a_same_thread_call_pumps_messages_rather_than_blocking_them(self) -> None:
        windows._message_loop_running = lambda: True
        pumps = {"n": 0}
        pending: dict[str, object] = {}

        class _DeferredTask:
            Result = "script-id"

            def ContinueWith(self, action, _scheduler):
                pending["fire"] = lambda: action(self)
                return self

        def fake_pump() -> None:
            pumps["n"] += 1
            # The native completion "arrives" only once the loop has actually
            # been pumped a few times — proving the wait needed the pumping,
            # not that it happened to succeed on the first check.
            if pumps["n"] >= 3 and "fire" in pending:
                pending.pop("fire")()

        windows._pump_messages = fake_pump

        result = windows._await_on_ui(browser=None, start_task=lambda: _DeferredTask(), timeout=2.0)

        self.assertEqual(result, "script-id")
        self.assertGreaterEqual(pumps["n"], 3, "must actually pump repeatedly, not poll once and give up")

    def test_a_same_thread_call_that_never_completes_times_out_rather_than_hanging(self) -> None:
        """The old bug's own timeout eventually fired too — 10s of 응답 없음 is
        still 응답 없음. Bounded failure is necessary here, not sufficient."""
        windows._message_loop_running = lambda: True
        windows._pump_messages = lambda: None  # the pending continuation never fires

        class _StuckTask:
            def ContinueWith(self, action, _scheduler):
                return self

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            windows._await_on_ui(browser=None, start_task=lambda: _StuckTask(), timeout=0.05)
        self.assertLess(time.monotonic() - started, 2.0, "must give up promptly, not hang for the full old timeout")

    def test_install_document_start_script_uses_the_same_thread_path_when_on_it(self) -> None:
        """End to end through the function pywebview's Shown/loaded events call
        directly — the exact call path that hung live."""
        windows._message_loop_running = lambda: True
        pumps = {"n": 0}

        def fake_pump() -> None:
            pumps["n"] += 1

        windows._pump_messages = fake_pump
        core = _FakeCore()
        browser = _FakeBrowser(core)
        original_browser = windows._browser
        windows._browser = lambda _window: browser
        windows._script_ids.clear()
        try:
            window = type("W", (), {"uid": "master"})()
            windows.install_document_start_script(window, "// v1")
            self.assertEqual(core.added, ["id-1"])
        finally:
            windows._browser = original_browser
            windows._script_ids.clear()


class Webview2RuntimeDetectionTests(unittest.TestCase):
    """Reported live: a healthy-looking 조작판 next to a blank 예매창.

    ensure_ready() previously only checked that the Python/.NET interop
    bindings load — bundled with the app, so they load regardless of whether
    the actual native WebView2 Runtime is installed. The runtime is only
    touched later, inside EnsureCoreWebView2Async, and pywebview's own handler
    for that failing is a bare logger.error with no exception — so the false
    "ready" reached the panel and the real failure reached nobody. This checks
    the registry directly instead, the same way Microsoft's own detection
    sample does.

    winreg does not exist on macOS, so it is fully faked here — a fake module
    installed into sys.modules, exercising _webview2_runtime_version's actual
    branching logic rather than skipping the test.
    """

    def setUp(self) -> None:
        self._real_winreg = sys.modules.get("winreg")

    def tearDown(self) -> None:
        if self._real_winreg is not None:
            sys.modules["winreg"] = self._real_winreg
        else:
            sys.modules.pop("winreg", None)

    def _install_fake_registry(self, entries: dict[tuple[int, str], str]):
        """entries maps (hive, subkey) -> the "pv" value, or absent if not found."""
        import types

        fake = types.ModuleType("winreg")
        fake.HKEY_LOCAL_MACHINE = 1
        fake.HKEY_CURRENT_USER = 2

        class _Key:
            def __init__(self, hive, subkey):
                self.hive, self.subkey = hive, subkey

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def OpenKey(hive, subkey):
            if (hive, subkey) not in entries:
                raise OSError("not found")
            return _Key(hive, subkey)

        def QueryValueEx(key, name):
            assert name == "pv"
            return entries[(key.hive, key.subkey)], 1

        fake.OpenKey = OpenKey
        fake.QueryValueEx = QueryValueEx
        sys.modules["winreg"] = fake

    def test_no_key_anywhere_means_not_installed(self) -> None:
        self._install_fake_registry({})
        self.assertEqual(windows._webview2_runtime_version(), "")

    def test_a_key_present_with_all_zeros_means_not_installed(self) -> None:
        """A key can exist without the runtime being there — not hypothetical,
        it is the exact case Microsoft's own detection sample guards against."""
        key = (1, f"SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients\\{windows._WEBVIEW2_CLIENT_GUID}")
        self._install_fake_registry({key: "0.0.0.0"})
        self.assertEqual(windows._webview2_runtime_version(), "")

    def test_a_real_version_in_the_machine_key_is_found(self) -> None:
        key = (1, f"SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{windows._WEBVIEW2_CLIENT_GUID}")
        self._install_fake_registry({key: "128.0.2739.42"})
        self.assertEqual(windows._webview2_runtime_version(), "128.0.2739.42")

    def test_a_per_user_install_with_no_admin_rights_is_still_found(self) -> None:
        key = (2, f"SOFTWARE\\Microsoft\\EdgeUpdate\\Clients\\{windows._WEBVIEW2_CLIENT_GUID}")
        self._install_fake_registry({key: "120.0.0.0"})
        self.assertEqual(windows._webview2_runtime_version(), "120.0.0.0")

    def test_ensure_ready_actually_calls_the_registry_check_and_raises_on_empty(self) -> None:
        """The exact gap this exists to close: bindings fine, runtime absent.

        ensure_ready() cannot be run end to end from here — its first line is
        `if platform.system() != "Windows": raise`, and pythonnet is not
        installed on this machine either, so any real call raises for reasons
        that have nothing to do with what changed. Source inspection is the
        honest check: that the registry lookup is actually wired into the
        guard, in the right order, with the right condition. Wiring a check no
        caller ever reaches is exactly as useless as not having it.
        """
        import inspect

        source = inspect.getsource(windows.ensure_ready)
        self.assertIn("_webview2_runtime_version()", source,
                     "ensure_ready must call the registry check, not just define it")
        self.assertIn("if not _webview2_runtime_version()", source,
                     "must raise specifically when the runtime is absent, not on any falsy value")


class SeamContractTests(unittest.TestCase):
    def test_both_backends_expose_the_same_interface(self) -> None:
        for name in ("ensure_ready", "lock_exclusive", "unlock",
                     "install_document_start_script", "cookie_store",
                     "dump_cookies", "restore_cookies", "timing_precision"):
            self.assertTrue(hasattr(darwin, name), f"darwin is missing {name}")
            self.assertTrue(hasattr(windows, name), f"windows is missing {name}")
            self.assertTrue(hasattr(app_platform, name), f"the seam is missing {name}")

    def test_both_backends_import_on_this_machine(self) -> None:
        """A typo in the Windows module must not wait until it reaches Windows."""
        self.assertEqual(darwin.NAME, "darwin")
        self.assertEqual(windows.NAME, "windows")

    # The regression that would quietly re-break Windows: a platform-only import
    # creeping back into shared code. `import fcntl` at module scope in
    # browser_bridge.py is what made the app unable to start there at all.
    FORBIDDEN = {
        "fcntl": "POSIX only",
        "WebKit": "macOS only",
        "Foundation": "macOS only",
        "objc": "macOS only",
        "PyObjCTools": "macOS only",
        "msvcrt": "Windows only",
    }

    def test_no_shared_module_imports_a_platform_only_library(self) -> None:
        allowed = {ROOT / "app_platform" / "darwin.py", ROOT / "app_platform" / "windows.py"}
        offenders: list[str] = []
        for path in sorted(list((ROOT / "mac").glob("*.py")) + list((ROOT / "core").glob("*.py"))):
            if path in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in self.FORBIDDEN:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} imports {name} "
                            f"({self.FORBIDDEN[name]})"
                        )
        self.assertEqual(offenders, [], "platform-only imports belong in app_platform:\n  "
                                       + "\n  ".join(offenders))

    # The regression that broke the Windows CI run on its first try:
    # `browser_host.py` is full of 한글 and em dashes, and `.read_text()` with no
    # `encoding=` decodes as cp1252 on Windows — a real Python default, not a
    # test artifact — so it raised `UnicodeDecodeError` on the exact file a test
    # needed to read. Every other `.read_text()`/`.write_text()` in the app
    # already names `encoding="utf-8"`; this keeps it that way.
    def test_no_read_or_write_text_omits_an_encoding(self) -> None:
        scan_dirs = [ROOT / "mac", ROOT / "core", ROOT / "tests", ROOT / "app_platform",
                    ROOT / "tools"]
        paths = sorted(p for d in scan_dirs if d.is_dir() for p in d.glob("*.py"))
        paths += sorted((ROOT).glob("*.py"))
        offenders: list[str] = []
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("read_text", "write_text")):
                    continue
                if any(kw.arg == "encoding" for kw in node.keywords):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} .{node.func.attr}()")
        self.assertEqual(offenders, [], "every text read/write needs encoding=\"utf-8\" — "
                                       "Windows' default is cp1252, not UTF-8:\n  "
                                       + "\n  ".join(offenders))


class PersistentDataTests(unittest.TestCase):
    """Persistent files must not live inside a frozen build's extraction dir.

    A PyInstaller one-file exe's `__file__` resolves inside `sys._MEIPASS`,
    which is deleted and recreated on every launch. The Naver session, saved
    seat preferences and the bridge state were all originally computed relative
    to `__file__` — correct for a source checkout, and silently wrong for the
    shipped exe: the whole point of porting cookie persistence to Windows would
    have been erased on the very next launch, with nothing to say so.

    `sys.frozen` is simulated by setting it directly (PyInstaller's own
    bootloader sets it the same way) and reversed in `tearDown`, since it is
    read by module-level code executed at import time.
    """

    def setUp(self) -> None:
        for name in ("browser_bridge", "browser_host", "pureclick"):
            sys.modules.pop(name, None)

    tearDown = setUp

    def test_a_source_checkout_keeps_writing_next_to_the_script(self) -> None:
        if hasattr(sys, "frozen"):
            del sys.frozen
        import browser_bridge

        bridge = browser_bridge.BrowserBridge(Path("/repo/mac"))
        self.assertEqual(bridge.state_path, Path("/repo/mac/.pureclick_browser_state.json"))
        self.assertEqual(bridge.health_path, Path("/repo/mac/.pureclick_bridge_health.json"))

    def test_a_frozen_build_moves_persistent_files_out_of_the_extraction_dir(self) -> None:
        sys.frozen = True
        try:
            import browser_bridge

            bridge = browser_bridge.BrowserBridge(Path("/tmp/_MEIxxxxxx/mac"))
            self.assertNotIn("_MEI", str(bridge.state_path),
                              "state_path must not resolve inside the wiped extraction dir")
            self.assertNotIn("_MEI", str(bridge.health_path))
            # host_script is unaffected — it is only read in the non-frozen
            # spawn branch, and must keep pointing at the real bundle layout.
            self.assertEqual(bridge.host_script, Path("/tmp/_MEIxxxxxx/mac/browser_host.py"))
        finally:
            del sys.frozen

    def test_the_cookie_path_moves_too(self) -> None:
        sys.frozen = True
        try:
            import browser_host

            self.assertNotIn("_MEI", str(browser_host.COOKIE_PATH))
        finally:
            del sys.frozen


class PlatformNoteTests(unittest.TestCase):
    """The self-check has to reach the screen, or it is not a self-check.

    Document-start injection is the one hook whose failure is invisible: the
    fallback still loads the autopilot on `loaded`, so the panel fills in and
    the app looks right while the popup shim is missing and the first few
    hundred milliseconds on the seat map are gone. On a machine neither of us
    can test, this line is the difference between "it doesn't work" and an
    answer.
    """

    def setUp(self) -> None:
        from core.seat import bridge_line, platform_note

        self.platform_note = platform_note
        self.bridge_line = bridge_line
        import time as _time

        self.now = _time.time()

    def _health(self, **over):
        health = {"seen_at": self.now, "last_ok": self.now, "failures": 0, "last_error": "",
                  "platform": "windows", "document_start": "ok",
                  "document_start_error": "", "autopilot_source": "bundled"}
        health.update(over)
        return health

    def test_a_working_run_says_nothing_extra(self) -> None:
        self.assertEqual(self.platform_note(self._health()), "")

    def test_a_failed_injection_is_named_on_the_line(self) -> None:
        note = self.platform_note(
            self._health(document_start="failed", document_start_error="no WebView2 yet")
        )
        self.assertIn("사전 주입 실패", note)
        self.assertIn("no WebView2 yet", note)

        line = self.bridge_line(
            self._health(document_start="failed", document_start_error="no WebView2 yet"),
            {"page": "seat"}, {}, now=self.now,
        )
        self.assertIn("사전 주입 실패", line, "the panel line must carry it, not just the helper")

    def test_an_unreadable_update_is_named(self) -> None:
        note = self.platform_note(self._health(autopilot_source="bundled (업데이트 읽기 실패: denied)"))
        self.assertIn("업데이트 읽기 실패", note)

    def test_a_normal_update_is_not_reported_as_trouble(self) -> None:
        self.assertEqual(self.platform_note(self._health(autopilot_source="updated")), "")

    def test_missing_health_is_not_a_crash(self) -> None:
        self.assertEqual(self.platform_note(None), "")
        self.assertEqual(self.platform_note({}), "")


class AtomicStateTests(unittest.TestCase):
    def test_a_reader_never_sees_a_half_written_state_file(self) -> None:
        """save_state was write_text: truncate, then fill.

        Two processes rewrite this file several times a second. A reader landing
        mid-write got a truncated document, load_state caught the JSONDecodeError
        and returned {}, and the whole panel state silently reset.

        Every real caller pairs load_state/save_state with locked_state — see
        read_state/write_state and apply_state in browser_host.py, patch_state
        here. That lock is the actual correctness guarantee on Windows: it is a
        mandatory OS lock there (msvcrt.locking), not advisory like flock, so a
        reader and a writer that both take it can never overlap, and
        _replace_atomic's retry is defense in depth rather than the thing this
        relies on. An earlier version of this test called load_state directly,
        unlocked, in a zero-delay busy loop across three threads — a contention
        pattern no real caller produces, and adversarial enough that it exhausted
        the retry budget on the Windows CI runner. Locked, the same contention is
        exercised honestly and reliably passes.
        """
        import tempfile

        from browser_bridge import load_state, locked_state, save_state

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            big = {"show_catalog": {"blocks": [{"k": i} for i in range(4000)]}}
            with locked_state(path):
                save_state(path, big)

            stop = threading.Event()
            torn: list[str] = []

            def reader() -> None:
                # A torn read makes load_state raise JSONDecodeError internally
                # and return {} — so an empty result IS the failure, and an
                # earlier version of this test skipped exactly that case with
                # `if state and ...`, which made it pass against the bug.
                while not stop.is_set():
                    with locked_state(path):
                        state = load_state(path)
                    if not state.get("show_catalog"):
                        torn.append("a reader saw no catalog — the file was mid-write")

            readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
            for t in readers:
                t.start()
            try:
                for n in range(150):
                    with locked_state(path):
                        save_state(
                            path, {"show_catalog": {"blocks": [{"k": i} for i in range(4000)], "n": n}}
                        )
            finally:
                stop.set()
                for t in readers:
                    t.join(timeout=5)

            self.assertEqual(torn, [], "os.replace must make the swap atomic")
            self.assertTrue(
                json.loads(path.read_text(encoding="utf-8"))["show_catalog"]["blocks"]
            )

    def test_a_transient_windows_sharing_violation_is_retried(self) -> None:
        """Measured on the Windows CI runner, not hypothetical.

        os.replace is MoveFileExW there, which refuses to replace a file another
        handle currently has open without FILE_SHARE_DELETE — the sharing mode
        plain open()/read_text() uses. A reader can be mid-open at the exact
        instant a writer replaces the state file, and PermissionError is exactly
        what that produced. It resolves within microseconds once the reader's
        handle closes, so a short backoff is correct; giving up on the first
        failure is not.
        """
        import tempfile

        import browser_bridge

        calls = {"n": 0}
        original_replace = browser_bridge.os.replace

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return original_replace(src, dst)

        original_sleep = browser_bridge.time.sleep
        browser_bridge.time.sleep = lambda _seconds: None  # the retry itself, not tested here
        browser_bridge.os.replace = flaky_replace
        try:
            with tempfile.TemporaryDirectory() as tmp:
                temp = Path(tmp) / "state.json.tmp"
                path = Path(tmp) / "state.json"
                temp.write_text("{}", encoding="utf-8")
                browser_bridge._replace_atomic(temp, path)
                self.assertEqual(
                    calls["n"], 3, "must retry past a transient failure, not raise on the first"
                )
                self.assertTrue(path.exists(), "the replace must still land once it succeeds")
        finally:
            browser_bridge.os.replace = original_replace
            browser_bridge.time.sleep = original_sleep

    def test_a_persistent_failure_still_raises(self) -> None:
        """The retry must not swallow a real, permanent failure forever."""
        import tempfile

        import browser_bridge

        original_replace = browser_bridge.os.replace
        original_sleep = browser_bridge.time.sleep
        browser_bridge.time.sleep = lambda _seconds: None
        browser_bridge.os.replace = lambda src, dst: (_ for _ in ()).throw(
            PermissionError(5, "Access is denied")
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                temp = Path(tmp) / "state.json.tmp"
                path = Path(tmp) / "state.json"
                temp.write_text("{}", encoding="utf-8")
                with self.assertRaises(PermissionError):
                    browser_bridge._replace_atomic(temp, path)
        finally:
            browser_bridge.os.replace = original_replace
            browser_bridge.time.sleep = original_sleep
