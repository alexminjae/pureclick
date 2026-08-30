"""Download the nol-ticket-web bundles for a page and mine their API surface.

Usage: python3 research/probes/mine_nol_api.py <url> [outdir]

Pulls every <script src> the page references, then extracts API paths, GraphQL
operation names, and route literals so the booking flow can be reconstructed
without guessing.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

SCRIPT_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
PATTERNS = {
    "api_paths": re.compile(r'"(/(?:ticket|onestop|api|v\d)[A-Za-z0-9_/{}$.\-]*)"'),
    "absolute_api": re.compile(r'"(https://[a-z0-9.\-]+\.(?:yanolja|interpark)\.com/[A-Za-z0-9_/{}$.\-]*)"'),
    "gql_ops": re.compile(r"(?:mutation|query)\s+([A-Z][A-Za-z0-9]+)"),
    "routes": re.compile(r'"(/ticket/[A-Za-z0-9_/\[\]\-]+)"'),
}


def fetch(url: str, timeout: float = 25.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        print(f"  ! {url} -> {error}", file=sys.stderr)
        return b""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    outdir = Path(sys.argv[2] if len(sys.argv) > 2 else "research/nol_mined")
    outdir.mkdir(parents=True, exist_ok=True)

    html = fetch(url).decode("utf-8", "replace")
    (outdir / "page.html").write_text(html, encoding="utf-8")
    print(f"page: {len(html)} bytes")

    srcs = []
    for raw in SCRIPT_RE.findall(html):
        srcs.append(urllib.parse.urljoin(url, raw))
    srcs = sorted(set(srcs))
    print(f"scripts: {len(srcs)}")

    def grab(src: str) -> tuple[str, str]:
        body = fetch(src).decode("utf-8", "replace")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", src.split("/")[-1])[:120]
        if body:
            (outdir / name).write_text(body, encoding="utf-8")
        return src, body

    with ThreadPoolExecutor(max_workers=10) as pool:
        bundles = list(pool.map(grab, srcs))

    found: dict[str, Counter] = {key: Counter() for key in PATTERNS}
    for _src, body in bundles:
        for key, pattern in PATTERNS.items():
            for match in pattern.findall(body):
                found[key][match] += 1

    report = outdir / "api_surface.txt"
    with report.open("w", encoding="utf-8") as handle:
        for key, counter in found.items():
            handle.write(f"\n===== {key} ({len(counter)}) =====\n")
            for value, count in sorted(counter.items()):
                handle.write(f"{count:5d}  {value}\n")
    print(f"wrote {report}")
    for key, counter in found.items():
        print(f"  {key}: {len(counter)} unique")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
