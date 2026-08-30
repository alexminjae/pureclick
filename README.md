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

**[mac/README.md](mac/README.md) is the manual** — how each function works, what
was measured, and what it will not do. This page is only the map.

## Layout

| | |
|---|---|
| `mac/` | the app — panel, browser host, bridge, session store |
| `browser/pureclick_autopilot.js` | everything that happens inside the booking page |
| `core/` | pure logic — no tkinter, no pywebview, no filesystem, all tested |
| `tests/` | `pytest tests/` and `node tests/test_autopilot_picker.mjs` |
| `research/probes/` | one script per measurement the design rests on |
| `research/seatmaps/`, `api_shapes/` | captured venue layouts and API response shapes |
| `docs/` | how the Interpark/NOL booking flow actually works |

## Tests

```bash
python3 -m pytest tests/ -q
node tests/test_autopilot_picker.mjs
```

## Legal

Korea's 공연법 (amended, effective March 2024) makes macro ticket purchasing an
offence when combined with resale — up to 1 year imprisonment or a ₩10M fine.
Automated booking also breaches the site's terms of use.
