#!/usr/bin/env python3
"""
Assemble a HAR file from the NDJSON entries written by `streaming_har_ndjson.py`.

Usage:
    ndjson_to_har.py INPUT.ndjson OUTPUT.har
    ndjson_to_har.py - -                       # stdin -> stdout
    ndjson_to_har.py live.ndjson -             # file -> stdout

Behavior:
  - Tolerates a truncated final line (e.g., mitmdump killed mid-write).
  - Sorts entries by startedDateTime (HAR spec says they SHOULD be in order).
  - Normalizes request.postData: if the addon stored base64 (binary body),
    decode to UTF-8 when possible; otherwise keep base64 with a `_encoding`
    marker (non-standard but preserves the payload).
  - Streams output: writes the HAR envelope and entries one at a time, so
    memory use stays O(1) in entry count when sorting is disabled (--no-sort).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import IO, Iterable, Iterator


HAR_CREATOR = {"name": "mitmproxy-streaming", "version": "1"}


def iter_entries(fh: IO[str]) -> Iterator[dict]:
    for lineno, line in enumerate(fh, 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield _normalize(json.loads(line))
        except json.JSONDecodeError as e:
            # Last line of a killed process may be truncated; warn and skip.
            print(
                f"warning: skipping malformed line {lineno}: {e}",
                file=sys.stderr,
            )


def _normalize(entry: dict) -> dict:
    req = entry.get("request") or {}
    post = req.get("postData")
    if isinstance(post, dict) and post.get("_encoding") == "base64":
        raw = base64.b64decode(post.get("text", ""))
        try:
            post["text"] = raw.decode("utf-8")
            post.pop("_encoding", None)
        except UnicodeDecodeError:
            # keep base64 + marker; non-standard but lossless
            pass
    return entry


def write_har(entries: Iterable[dict], out: IO[str], pretty: bool) -> int:
    indent = 2 if pretty else None
    sep = (",", ": ") if pretty else (",", ":")

    # Hand-roll the envelope so we can stream entries without holding the list.
    out.write('{"log":{"version":"1.2","creator":')
    json.dump(HAR_CREATOR, out, separators=sep)
    out.write(',"entries":[')

    n = 0
    for entry in entries:
        if n:
            out.write(",")
        if pretty:
            out.write("\n")
        json.dump(entry, out, indent=indent, separators=sep)
        n += 1

    if pretty and n:
        out.write("\n")
    out.write("]}}")
    if pretty:
        out.write("\n")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("input", help="NDJSON path, or - for stdin")
    p.add_argument("output", help="HAR path, or - for stdout")
    p.add_argument(
        "--no-sort",
        action="store_true",
        help="Stream without sorting by startedDateTime (lower memory).",
    )
    p.add_argument("--pretty", action="store_true", help="Indented HAR output.")
    args = p.parse_args()

    in_fh = sys.stdin if args.input == "-" else open(args.input, "r", encoding="utf-8")
    out_fh = (
        sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8")
    )

    try:
        if args.no_sort:
            entries: Iterable[dict] = iter_entries(in_fh)
        else:
            entries = sorted(
                iter_entries(in_fh),
                key=lambda e: e.get("startedDateTime", ""),
            )
        write_har(entries, out_fh, args.pretty)
    finally:
        if in_fh is not sys.stdin:
            in_fh.close()
        if out_fh is not sys.stdout:
            out_fh.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
