"""Reassemble a Next.js App Router RSC payload and pull out embedded objects.

NOL product pages stream their data as `self.__next_f.push([1,"<chunk>"])`
fragments. Concatenating the decoded chunks yields the flight stream, which
contains the full goodsDetail / playSeq / price JSON that the page renders from.

Usage: python3 research/probes/extract_rsc.py <page.html> [key ...]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')


def reassemble(html: str) -> str:
    parts = []
    for raw in PUSH_RE.findall(html):
        try:
            parts.append(json.loads(f'"{raw}"'))
        except json.JSONDecodeError:
            parts.append(raw)
    return "".join(parts)


def find_objects(stream: str, key: str) -> list[dict]:
    """Return every JSON object in `stream` that contains `"key":` at its top level."""
    results = []
    needle = f'"{key}"'
    for match in re.finditer(re.escape(needle), stream):
        start = stream.rfind("{", 0, match.start())
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, min(len(stream), start + 400_000)):
                char = stream[index]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        blob = stream[start : index + 1]
                        try:
                            parsed = json.loads(blob)
                        except json.JSONDecodeError:
                            break
                        if isinstance(parsed, dict) and key in parsed:
                            results.append(parsed)
                        break
            break
    unique = []
    seen = set()
    for item in results:
        stamp = json.dumps(item, sort_keys=True)[:2000]
        if stamp not in seen:
            seen.add(stamp)
            unique.append(item)
    return unique


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    html = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    stream = reassemble(html)
    out = Path(sys.argv[1]).with_suffix(".rsc.txt")
    out.write_text(stream, encoding="utf-8")
    print(f"stream: {len(stream)} chars -> {out}")

    keys = sys.argv[2:] or ["onestopEnabled", "goodsCode"]
    for key in keys:
        objects = find_objects(stream, key)
        print(f"\n===== {key}: {len(objects)} object(s)")
        for obj in objects[:3]:
            scalars = {
                name: value
                for name, value in obj.items()
                if isinstance(value, (str, int, float, bool, type(None)))
                and len(str(value)) < 90
            }
            print(json.dumps(scalars, ensure_ascii=False, indent=2)[:2600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
