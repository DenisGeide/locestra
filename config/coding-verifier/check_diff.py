from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_diff.py PATCH", file=sys.stderr)
        return 2
    payload = Path(sys.argv[1]).read_bytes()
    failures: list[str] = []
    in_hunk = False
    target_line = 0
    for line in payload.splitlines(keepends=True):
        if line.startswith(b"@@"):
            in_hunk = True
            try:
                target = line.split(b"+", 1)[1].split(b" ", 1)[0]
                target_line = int(target.split(b",", 1)[0])
            except (IndexError, ValueError):
                print("malformed unified diff hunk", file=sys.stderr)
                return 2
            continue
        if not in_hunk:
            continue
        if line.startswith(b"diff --git ") or line.startswith(b"@@"):
            in_hunk = line.startswith(b"@@")
            continue
        if line.startswith(b"+") and not line.startswith(b"+++"):
            content = line[1:].rstrip(b"\r\n")
            if content.endswith((b" ", b"\t")):
                failures.append(f"line {target_line}: trailing whitespace")
            indentation = content[: len(content) - len(content.lstrip(b" \t"))]
            if b"\t" in indentation and indentation.startswith(b" "):
                failures.append(f"line {target_line}: space before tab in indent")
            target_line += 1
        elif line.startswith(b" "):
            target_line += 1
        elif line.startswith(b"-") and not line.startswith(b"---"):
            continue
        elif line.startswith(b"\\ No newline at end of file"):
            continue
        else:
            in_hunk = False
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
