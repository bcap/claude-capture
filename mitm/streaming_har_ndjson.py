"""
Streaming HAR-entry writer for mitmproxy.

Writes one HAR `log.entries[]` object per line (NDJSON) as each flow completes.
The file is not a valid HAR until passed through `ndjson_to_har.py`.

Usage:
    mitmdump -s streaming_har_ndjson.py --set har_ndjson=/tmp/live.ndjson
"""

import base64
import json
import os
import time
from typing import Optional, TextIO

from mitmproxy import ctx, http


def _iso(ts: float) -> str:
    secs = int(ts)
    ms = int((ts - secs) * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{ms:03d}Z"


# Headers stripped from serialized entries because we store the *decoded* body
# (HAR `content.text` is spec'd as decoded). Leaving these in would make any HAR
# loader either double-decode and fail, or report a length that no longer matches.
_STRIP_HEADERS = frozenset({"content-encoding", "content-length"})


def _headers(h) -> list:
    return [
        {"name": k, "value": v}
        for k, v in h.items()
        if k.lower() not in _STRIP_HEADERS
    ]


def _decoded(message) -> bytes:
    # get_content(strict=False) decodes Content-Encoding; on decode failure it
    # falls back to raw bytes instead of raising, which keeps partial/garbled
    # bodies recoverable rather than dropping the entry.
    try:
        body = message.get_content(strict=False)
    except (ValueError, OSError):
        body = message.raw_content
    return body or b""


def _cookies_req(req) -> list:
    return [{"name": k, "value": v} for k, v in req.cookies.items(multi=True)]


def _cookies_resp(resp) -> list:
    out = []
    for k, (v, attrs) in resp.cookies.items(multi=True):
        c = {"name": k, "value": v}
        if "path" in attrs:
            c["path"] = attrs["path"]
        if "domain" in attrs:
            c["domain"] = attrs["domain"]
        if "expires" in attrs:
            c["expires"] = attrs["expires"]
        c["httpOnly"] = "httponly" in (a.lower() for a in attrs)
        c["secure"] = "secure" in (a.lower() for a in attrs)
        out.append(c)
    return out


def _content(message) -> dict:
    body = _decoded(message)
    mime = message.headers.get("content-type", "")
    return {
        "size": len(body),
        "mimeType": mime,
        "text": base64.b64encode(body).decode("ascii"),
        "encoding": "base64",
    }


class StreamingHARNDJSON:
    def __init__(self) -> None:
        self.path: Optional[str] = None
        self.fh: Optional[TextIO] = None

    def load(self, loader) -> None:
        loader.add_option(
            "har_ndjson", str, "", "Path to NDJSON file of HAR entries (streaming)."
        )

    def configure(self, updates) -> None:
        if "har_ndjson" not in updates:
            return
        if self.fh is not None:
            try:
                self.fh.close()
            finally:
                self.fh = None
        self.path = ctx.options.har_ndjson or None
        if self.path:
            # line-buffered so each entry is flushed to disk on newline
            self.fh = open(self.path, "a", buffering=1, encoding="utf-8")

    def done(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None

    def response(self, flow: http.HTTPFlow) -> None:
        self._write(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        # Partial flow: no response, still useful to record the request.
        self._write(flow)

    def _write(self, flow: http.HTTPFlow) -> None:
        if self.fh is None or flow.request is None:
            return
        req = flow.request
        resp = flow.response

        started = flow.timestamp_start or req.timestamp_start or time.time()
        end = (
            resp.timestamp_end if resp and resp.timestamp_end else req.timestamp_end
        ) or started

        entry = {
            "startedDateTime": _iso(started),
            "time": max(0, int((end - started) * 1000)),
            "request": {
                "method": req.method,
                "url": req.url,
                "httpVersion": req.http_version,
                "headers": _headers(req.headers),
                "queryString": [
                    {"name": k, "value": v} for k, v in req.query.items(multi=True)
                ],
                "cookies": _cookies_req(req),
                "headersSize": -1,
                "bodySize": len(req.raw_content or b""),
                "postData": (
                    {
                        "mimeType": req.headers.get("content-type", ""),
                        "text": base64.b64encode(_decoded(req)).decode("ascii"),
                        "_encoding": "base64",
                    }
                    if req.raw_content
                    else None
                ),
            },
            "response": (
                {
                    "status": resp.status_code,
                    "statusText": resp.reason or "",
                    "httpVersion": resp.http_version,
                    "headers": _headers(resp.headers),
                    "cookies": _cookies_resp(resp),
                    "redirectURL": resp.headers.get("location", ""),
                    "headersSize": -1,
                    "bodySize": len(resp.raw_content or b""),
                    "content": _content(resp),
                }
                if resp
                else {
                    "status": 0,
                    "statusText": "",
                    "httpVersion": "",
                    "headers": [],
                    "cookies": [],
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": -1,
                    "content": {"size": 0, "mimeType": "", "text": "", "encoding": "base64"},
                }
            ),
            "cache": {},
            "timings": {
                "send": 0,
                "wait": int(
                    max(
                        0,
                        ((resp.timestamp_start if resp else end) - (req.timestamp_end or started))
                        * 1000,
                    )
                )
                if resp and req.timestamp_end
                else 0,
                "receive": int(
                    max(0, ((resp.timestamp_end - resp.timestamp_start) * 1000))
                )
                if resp and resp.timestamp_start and resp.timestamp_end
                else 0,
            },
            "serverIPAddress": flow.server_conn.peername[0]
            if flow.server_conn and flow.server_conn.peername
            else "",
            "_clientAddress": flow.client_conn.peername[0]
            if flow.client_conn and flow.client_conn.peername
            else "",
            "_error": flow.error.msg if flow.error else None,
        }

        # Drop None postData to keep the entry clean.
        if entry["request"]["postData"] is None:
            del entry["request"]["postData"]

        self.fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        # line buffering already flushes; explicit flush is belt-and-suspenders:
        self.fh.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass


addons = [StreamingHARNDJSON()]
