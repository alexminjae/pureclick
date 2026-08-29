# PureClick

Interpark / NOL ticket assistant — **Mac NOL Sniper** (embedded browser) and **Windows** (native click + screen watch).

| Edition | Folder | Run |
|---|---|---|
| **Mac** | `mac/` | `./mac/run_pureclick.sh` |
| **Windows** | repo root | `run_pureclick.bat` |

## Mac · API-driven (recommended for testing)

Embedded browser with waiting-API queue entry and seat select autopilot. See [mac/README.md](mac/README.md).

```bash
cd mac && ./run_pureclick.sh
```

## Windows · Native click + screen watch

Phase 1 timed click + Phase 2 color-change cancellation watch. See root `pureclick.py`.

```powershell
python pureclick.py
```

## Docs

[docs/interpark_flow.md](docs/interpark_flow.md) — API endpoints and booking flow.

