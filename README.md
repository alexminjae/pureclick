# PureClick

NOL / Interpark ticket assistant for macOS. Two jobs, and nothing else:

- **오픈 대기** — be first into the queue the instant a show opens
- **취켓팅** — watch an area of the seat map you drew and take whatever
  cancellation appears in it, at any grade

## Run

```bash
cd mac && ./run_pureclick.sh
```

Two windows open: the 조작판 (control panel) and the 예매 창 (an embedded
browser). You log in and type any 보안문자 yourself in the 예매 창; everything
else is driven from inside that page.

## Shape

| | |
|---|---|
| `mac/pureclick.py` | the panel — tkinter, the server clock, the arm scheduler |
| `mac/browser_host.py` | the 예매 창 — pywebview, injects the autopilot at document start |
| `browser/pureclick_autopilot.js` | everything that happens inside the page |
| `pureclick_*.py` | pure logic, at the repo root, shared by both and unit-tested |
| `research/` | probes and captured site data behind the decisions above |

The panel and the browser host are separate processes that talk through a
flock-guarded JSON file (`mac/.pureclick_browser_state.json`). Measured at
0.55 ms a read, which is why it is a file and not something cleverer.

## Tests

```bash
python3 -m pytest tests/ -q          # the Python side
node tests/test_autopilot_picker.mjs # the in-page logic
```

## Windows

Removed. It was a native screen-clicker with a colour-change watch, and it had
been superseded by the embedded-browser design — it could not run on macOS, and
its tests were still counted in the suite. It is in the history if it is ever
wanted back.
