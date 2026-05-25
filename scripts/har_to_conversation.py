#!/usr/bin/env python3
"""
Reconstruct the user<->assistant conversation captured in a HAR file.

Usage:
    har_to_conversation.py INPUT.har[.zst|.xz|.gz]            # -> stdout
    har_to_conversation.py INPUT.har.zst -o conversation.md
    har_to_conversation.py -                                  # stdin

Behavior:
  - Reads all POSTs to `/v1/messages` from the HAR (sorted by time) and parses
    each request body (the full message history at that point) and each
    response body (an SSE stream of the assistant reply).
  - Builds a tree of messages, merging shared prefixes across requests. The
    Claude Code TUI lets users rewind a turn and branch, so the conversation
    is not always linear.
  - Emits each content block (user text, tool_result, assistant text,
    thinking, tool_use) as its own `# <actor> on <YYYY-MM-DD HH:MM:SS>` entry,
    separated by `---------`. Branch points are marked with `=========`.
  - Filters out quota-probe requests (single "quota" user message with
    max_tokens<=8) so they don't appear as a spurious top-level branch.
  - With `--tokens`, prints a second `# tokens: in=… out=… cache_read=…
    cache_write=…` header line on the first block of each assistant turn,
    extracted from the SSE `message_start`/`message_delta` events.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import io
import json
import lzma
import sys
from datetime import datetime
from typing import IO, Optional


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def open_har(path: str) -> IO[bytes]:
    if path == "-":
        return io.BytesIO(sys.stdin.buffer.read())
    if path.endswith(".zst"):
        try:
            import zstandard  # type: ignore
        except ImportError:
            sys.exit("error: zstandard module required to read .zst (pip install zstandard)")
        with open(path, "rb") as f:
            return io.BytesIO(zstandard.ZstdDecompressor().stream_reader(f).read())
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def decode_body(content: dict) -> str:
    text = content.get("text", "") or ""
    if content.get("encoding") == "base64":
        text = base64.b64decode(text).decode("utf-8", errors="replace")
    return text


def decode_post_data(post_data: dict) -> str:
    text = post_data.get("text", "") or ""
    if post_data.get("_encoding") == "base64":
        text = base64.b64decode(text).decode("utf-8", errors="replace")
    return text


# ---------------------------------------------------------------------------
# SSE → assistant content blocks
# ---------------------------------------------------------------------------

def parse_sse_assistant(sse_text: str) -> tuple[list[dict], Optional[dict]]:
    """Reconstruct (content[], usage) from an SSE stream.

    Usage prefers `message_delta` (final, has end-of-turn output_tokens) and
    falls back to `message_start`.
    """
    blocks: dict[int, dict] = {}
    order: list[int] = []
    usage: Optional[dict] = None
    for raw in sse_text.split("\n\n"):
        data_lines = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
        if not data_lines:
            continue
        try:
            evt = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        et = evt.get("type")
        if et == "message_start":
            u = (evt.get("message") or {}).get("usage")
            if u and usage is None:
                usage = dict(u)
        elif et == "message_delta":
            u = evt.get("usage")
            if u:
                usage = dict(u)
        elif et == "content_block_start":
            idx = evt["index"]
            block = dict(evt["content_block"])
            if block.get("type") == "tool_use":
                block["_partial_json"] = ""
            blocks[idx] = block
            order.append(idx)
        elif et == "content_block_delta":
            idx = evt["index"]
            block = blocks.get(idx)
            if block is None:
                continue
            d = evt.get("delta", {})
            dt = d.get("type")
            if dt == "text_delta":
                block["text"] = block.get("text", "") + d.get("text", "")
            elif dt == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                block["signature"] = block.get("signature", "") + d.get("signature", "")
            elif dt == "input_json_delta":
                block["_partial_json"] = block.get("_partial_json", "") + d.get("partial_json", "")
        elif et == "content_block_stop":
            idx = evt.get("index")
            block = blocks.get(idx)
            if block and block.get("type") == "tool_use":
                pj = block.pop("_partial_json", "")
                try:
                    block["input"] = json.loads(pj) if pj else {}
                except json.JSONDecodeError:
                    block["input"] = {"_raw_partial_json": pj}
    return [blocks[i] for i in order if i in blocks], usage


# ---------------------------------------------------------------------------
# Conversation tree
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("msg", "ts", "children", "usage")

    def __init__(self, msg: Optional[dict], ts: str, usage: Optional[dict] = None):
        self.msg = msg
        self.ts = ts
        self.usage = usage
        self.children: list[Node] = []


def msg_key(msg: dict) -> str:
    return json.dumps(msg, sort_keys=True)


def insert_path(
    root: Node,
    messages: list[dict],
    assistant_content: list[dict],
    ts: str,
    usage: Optional[dict] = None,
) -> None:
    cur = root
    for m in messages:
        k = msg_key(m)
        nxt = next((c for c in cur.children if msg_key(c.msg) == k), None)
        if nxt is None:
            nxt = Node(m, ts)
            cur.children.append(nxt)
        cur = nxt
    if assistant_content:
        synth = {"role": "assistant", "content": assistant_content}
        k = msg_key(synth)
        nxt = next((c for c in cur.children if msg_key(c.msg) == k), None)
        if nxt is None:
            nxt = Node(synth, ts, usage=usage)
            cur.children.append(nxt)
        elif usage and nxt.usage is None:
            nxt.usage = usage


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def fmt_ts(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        return datetime.strptime(iso_ts[:19], "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_ts


def write_entry(out: IO[str], actor: str, ts: str, body: str, extra_header: str = "") -> None:
    out.write("---------\n\n")
    out.write(f"# {actor} on {fmt_ts(ts)}\n")
    if extra_header:
        out.write(extra_header if extra_header.endswith("\n") else extra_header + "\n")
    out.write("\n")
    if body:
        out.write(body if body.endswith("\n") else body + "\n")
    out.write("\n")


def fmt_usage(usage: dict) -> str:
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cr = usage.get("cache_read_input_tokens", 0)
    cw = usage.get("cache_creation_input_tokens", 0)
    return f"# tokens: in={inp} out={out} cache_read={cr} cache_write={cw}"


def tool_result_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for sub in content:
            if isinstance(sub, dict):
                t = sub.get("type")
                if t == "text":
                    parts.append(sub.get("text", ""))
                elif t == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(sub))
            else:
                parts.append(str(sub))
        return "\n".join(parts)
    return json.dumps(content)


def emit_message(out: IO[str], msg: dict, ts: str, extra_header_first: str = "") -> None:
    role = msg.get("role", "?")
    content = msg.get("content", "")
    pending = extra_header_first  # consumed by the first entry only
    if isinstance(content, str):
        write_entry(out, role, ts, content, extra_header=pending)
        return
    for block in content:
        eh, pending = pending, ""
        if not isinstance(block, dict):
            write_entry(out, role, ts, str(block), extra_header=eh)
            continue
        bt = block.get("type")
        if bt == "text":
            write_entry(out, role, ts, block.get("text", ""), extra_header=eh)
        elif bt == "thinking":
            write_entry(out, f"{role} (thinking)", ts, block.get("thinking", ""), extra_header=eh)
        elif bt == "tool_use":
            name = block.get("name", "?")
            tid = block.get("id", "")
            inp = block.get("input", {})
            body = f"id: {tid}\ninput:\n{json.dumps(inp, indent=2, ensure_ascii=False)}"
            write_entry(out, f"{role} (tool_use: {name})", ts, body, extra_header=eh)
        elif bt == "tool_result":
            tid = block.get("tool_use_id", "")
            err = " error" if block.get("is_error") else ""
            body = tool_result_to_text(block.get("content", ""))
            write_entry(out, f"{role} (tool_result{err}: {tid})", ts, body, extra_header=eh)
        elif bt == "image":
            write_entry(out, role, ts, "[image]", extra_header=eh)
        else:
            write_entry(out, f"{role} ({bt})", ts, json.dumps(block, ensure_ascii=False), extra_header=eh)


def walk(out: IO[str], node: Node, show_tokens: bool = False) -> None:
    if node.msg is not None:
        eh = fmt_usage(node.usage) if (show_tokens and node.usage) else ""
        emit_message(out, node.msg, node.ts, extra_header_first=eh)
    children = node.children
    if not children:
        return
    if len(children) == 1:
        walk(out, children[0], show_tokens)
        return
    ordered = sorted(children, key=lambda c: c.ts)
    n = len(ordered)
    for i, child in enumerate(ordered, 1):
        out.write(f"=========\n\n(branch {i} of {n}, from above)\n\n")
        walk(out, child, show_tokens)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def is_quota_probe(data: dict) -> bool:
    if data.get("max_tokens", 0) > 8:
        return False
    msgs = data.get("messages", [])
    if len(msgs) != 1:
        return False
    c = msgs[0].get("content")
    return c == "quota" or (
        isinstance(c, list) and len(c) == 1
        and isinstance(c[0], dict)
        and c[0].get("type") == "text"
        and c[0].get("text") == "quota"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reconstruct the conversation captured in a HAR file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("har_file", help="HAR path (.har, .har.zst, .har.xz, .har.gz, or - for stdin)")
    ap.add_argument("-o", "--output", default="-", help="Output path (default stdout)")
    ap.add_argument(
        "-t", "--tokens",
        action="store_true",
        help="Print per-turn token usage (in/out/cache_read/cache_write) under each assistant header",
    )
    args = ap.parse_args()

    with open_har(args.har_file) as fp:
        har = json.load(fp)

    entries = har.get("log", {}).get("entries", [])
    posts = [
        e for e in entries
        if e.get("request", {}).get("method") == "POST"
        and "/v1/messages" in e.get("request", {}).get("url", "")
        and e.get("response", {}).get("status") == 200
    ]
    posts.sort(key=lambda e: e.get("startedDateTime", ""))

    root = Node(None, "")
    for e in posts:
        try:
            req = json.loads(decode_post_data(e["request"].get("postData", {}) or {}))
        except (json.JSONDecodeError, ValueError):
            continue
        if is_quota_probe(req):
            continue
        messages = req.get("messages") or []
        if not messages:
            continue
        sse = decode_body(e.get("response", {}).get("content", {}) or {})
        assistant_content, usage = parse_sse_assistant(sse)
        insert_path(root, messages, assistant_content, e["startedDateTime"], usage=usage)

    out = sys.stdout if args.output == "-" else open(args.output, "w", encoding="utf-8")
    try:
        if not root.children:
            return
        if len(root.children) == 1:
            walk(out, root.children[0], args.tokens)
        else:
            ordered = sorted(root.children, key=lambda c: c.ts)
            n = len(ordered)
            for i, child in enumerate(ordered, 1):
                out.write(f"=========\n\n(top-level branch {i} of {n})\n\n")
                walk(out, child, args.tokens)
    finally:
        if out is not sys.stdout:
            out.close()


if __name__ == "__main__":
    main()
