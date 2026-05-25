#!/usr/bin/env python3
"""
Render a HAR file from stdin as curl -v style request/response pairs.

Usage:
    har_to_wire.py < capture.har
    zstd -dc capture.har.zst | har_to_wire.py
    har_to_wire.py --max-body 4096 < capture.har

Format per entry (curl -v conventions):
  * info / timing lines
  > request line + headers
  > (blank)
  request body (raw, no prefix)
  < response status + headers
  < (blank)
  response body (raw, no prefix)

Bodies stored base64 in the HAR (binary or per the capture addon's marker)
are decoded; if the bytes are valid UTF-8 they're printed verbatim, otherwise
a `[N bytes binary]` placeholder is shown.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from typing import IO, Iterable, TypedDict

__all__ = ["main"]


class Header(TypedDict):
    name: str
    value: str


class PostData(TypedDict, total=False):
    mimeType: str
    text: str
    _encoding: str  # non-standard marker written by streaming_har_ndjson addon


class Request(TypedDict, total=False):
    method: str
    url: str
    httpVersion: str
    headers: list[Header]
    postData: PostData
    bodySize: int


class Content(TypedDict, total=False):
    size: int
    mimeType: str
    text: str
    encoding: str


class Response(TypedDict, total=False):
    status: int
    statusText: str
    httpVersion: str
    headers: list[Header]
    content: Content
    bodySize: int


class Timings(TypedDict, total=False):
    send: float
    wait: float
    receive: float
    blocked: float
    dns: float
    connect: float
    ssl: float


class Entry(TypedDict, total=False):
    startedDateTime: str
    time: float
    request: Request
    response: Response
    timings: Timings
    serverIPAddress: str
    connection: str


def _iter_entries(fh: IO[str]) -> Iterable[Entry]:
    data = json.load(fh)
    log = data.get("log") or {}
    entries = log.get("entries") or []
    if not isinstance(entries, list):
        raise ValueError("HAR log.entries is not a list")
    return entries


def _decode_body(text: str | None, encoding: str | None) -> tuple[str, int, bool]:
    """Return (rendered_text, byte_size, is_binary_placeholder)."""
    if text is None:
        return "", 0, False
    if encoding == "base64":
        try:
            raw = base64.b64decode(text)
        except (ValueError, base64.binascii.Error) as e:
            raise ValueError(f"invalid base64 body: {e}") from e
        try:
            return raw.decode("utf-8"), len(raw), False
        except UnicodeDecodeError:
            return f"[{len(raw)} bytes binary]", len(raw), True
    return text, len(text.encode("utf-8", errors="replace")), False


def _truncate(body: str, limit: int) -> str:
    if limit <= 0 or len(body) <= limit:
        return body
    return body[:limit] + f"\n... [truncated, {len(body) - limit} chars elided]"


def _write_headers(out: IO[str], prefix: str, headers: list[Header]) -> None:
    for h in headers or []:
        out.write(f"{prefix}{h.get('name', '')}: {h.get('value', '')}\n")


def _fmt_timings(t: Timings) -> str:
    parts = []
    for k in ("blocked", "dns", "connect", "ssl", "send", "wait", "receive"):
        v = t.get(k)
        if v is not None and v >= 0:
            parts.append(f"{k}={v:.1f}ms")
    return ", ".join(parts)


def _render_entry(
    out: IO[str],
    entry: Entry,
    show_bodies: bool,
    max_body: int,
) -> None:
    req = entry.get("request") or {}
    resp = entry.get("response") or {}
    timings = entry.get("timings") or {}

    started = entry.get("startedDateTime", "")
    total = entry.get("time")
    server_ip = entry.get("serverIPAddress")
    connection = entry.get("connection")

    if started:
        out.write(f"* Started: {started}\n")
    if server_ip:
        suffix = f" (connection {connection})" if connection else ""
        out.write(f"* Server IP: {server_ip}{suffix}\n")
    if total is not None:
        out.write(f"* Total: {total:.1f}ms")
        breakdown = _fmt_timings(timings)
        if breakdown:
            out.write(f" ({breakdown})")
        out.write("\n")

    method = req.get("method", "")
    url = req.get("url", "")
    http_version = req.get("httpVersion") or "HTTP/1.1"
    out.write(f"> {method} {url} {http_version}\n")
    _write_headers(out, "> ", req.get("headers", []))
    out.write(">\n")

    if show_bodies:
        post = req.get("postData") or {}
        body, size, _ = _decode_body(post.get("text"), post.get("_encoding"))
        if body:
            out.write(_truncate(body, max_body))
            if not body.endswith("\n"):
                out.write("\n")
        elif req.get("bodySize", 0) > 0:
            out.write(f"[request body: {req.get('bodySize')} bytes, not captured]\n")

    status = resp.get("status", 0)
    status_text = resp.get("statusText", "")
    resp_version = resp.get("httpVersion") or "HTTP/1.1"
    out.write(f"< {resp_version} {status} {status_text}\n")
    _write_headers(out, "< ", resp.get("headers", []))
    out.write("<\n")

    if show_bodies:
        content = resp.get("content") or {}
        body, _, _ = _decode_body(content.get("text"), content.get("encoding"))
        if body:
            out.write(_truncate(body, max_body))
            if not body.endswith("\n"):
                out.write("\n")
        elif content.get("size", 0) > 0:
            out.write(f"[response body: {content.get('size')} bytes, not captured]\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Render HAR (stdin) as curl -v style request/response pairs."
    )
    p.add_argument(
        "--no-bodies", action="store_true", help="Omit request and response bodies."
    )
    p.add_argument(
        "--max-body",
        type=int,
        default=0,
        metavar="N",
        help="Truncate bodies longer than N chars (0 = no limit, default).",
    )
    args = p.parse_args()

    try:
        entries = _iter_entries(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"error: invalid HAR JSON on stdin: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    first = True
    for entry in entries:
        if not first:
            sys.stdout.write("\n" + ("-" * 72) + "\n\n")
        first = False
        _render_entry(sys.stdout, entry, show_bodies=not args.no_bodies, max_body=args.max_body)

    return 0


if __name__ == "__main__":
    sys.exit(main())
