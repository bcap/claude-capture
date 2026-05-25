# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tiny tooling repo that wraps the `claude` CLI with an mitmproxy-based HTTP(S) capture so every conversation's network traffic is recorded as a HAR file. Uses `mitmweb` (not `mitmdump`) so the live capture is also browsable in a local web UI during the run. No build system, no tests, no package manifest — just three Python scripts and a Bash wrapper.

## Commands

- Run claude with capture: `./claude-capture [args forwarded to claude]`
  - Optional env vars (all `CAPTURE_*`-prefixed):
    - `CAPTURE_MITM_FLAGS='...'` — extra flags forwarded to `mitmweb` (word-split, no shell-quote parsing).
    - `CAPTURE_FILE_FORMAT='...'` — full output filename, passed verbatim to `date +<format>` so strftime tokens expand. Default `.claude-traffic-%Y%m%d-%H%M%S.har`. Do not include the compression suffix; it is appended automatically.
    - `CAPTURE_COMPRESS=0|1|true|false|yes|no|on|off` — toggle compression. Default `1` (enabled). When disabled the raw `.har` is left in place.
  - Produces `<CAPTURE_FILE_FORMAT>[.zst|.xz|.gz]` in the current working directory.
  - Acts as a drop-in `claude` replacement: forwards args via `"$@"`, propagates claude's exit code, and `exec`s claude directly if `mitmweb` is missing.
- Convert a saved NDJSON capture to HAR manually: `./scripts/ndjson_to_har.py INPUT.ndjson OUTPUT.har` (use `-` for stdin/stdout; `--pretty` for indented; `--no-sort` to stream without holding entries in memory).
- Reconstruct the conversation from a HAR: `./scripts/har_to_conversation.py INPUT.har[.zst|.xz|.gz] [-o OUT.md]`. Walks every `/v1/messages` POST (request body = history, response body = SSE stream parsed back into content blocks), merges shared prefixes across requests into a tree, and emits each text / thinking / tool_use / tool_result block as a separate `# <actor> on <ts>` entry. Branch points (rewind/edit-turn in the TUI) are marked with `=========`.

There are no tests or linters configured. Don't add a CI/test scaffold unless explicitly asked.

## Architecture

The five pieces have a strict pipeline relationship — changing one usually means touching another:

1. **`claude-capture`** — orchestrator. Pre-allocates an ephemeral web-UI port via a Python socket-bind trick (mitmweb has no way to report a `--web-port 0` choice back to the wrapper), generates a random hex token and passes it via `--set web_password=$WEB_TOKEN` (so the wrapper can print the full `?token=...` URL — there is no way to read mitmweb's own randomly generated token from outside its internals), starts `mitmweb -p 0 --web-host 127.0.0.1 --web-port $WEB_PORT --no-web-open-browser` with stdout+stderr redirected to a temp log (so claude's TUI is not corrupted; log is dumped on early failure and discarded on success), loads the two addons, polls a temp port-file (≤10s) for the bound proxy port, exports `HTTPS_PROXY` / `HTTP_PROXY` / `NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem`, runs `claude`, then shuts mitmweb down via `graceful_stop` (SIGTERM → 5s wait → SIGKILL → `wait` to reap), runs `scripts/ndjson_to_har.py`, and compresses with the first available of zstd → xz → pigz → gzip.

2. **`mitm/streaming_har_ndjson.py`** — mitmproxy addon. On every `response`/`error` hook, appends one fully-formed HAR `log.entries[]` object as a single line to `--set har_ndjson=PATH`, then fsyncs. This is the durability contract: a SIGKILL mid-session loses at most the in-flight flow, never the file. Bodies are stored **decoded** (Content-Encoding applied via `get_content(strict=False)`) and base64-encoded in `response.content` (standard HAR) and, for non-trivial request bodies, in a non-standard `request.postData._encoding="base64"` marker. `Content-Encoding` and `Content-Length` are stripped from the serialized headers because they no longer match the stored body — leaving them in causes HAR viewers (including mitmweb's own `-r` reload) to double-decode and render garbage. `response.bodySize` is kept at the on-wire byte count; `content.size` reflects the decoded length.

3. **`mitm/port_writer.py`** — mitmproxy addon. In `running()` (fires once the proxy is bound), atomically writes the listen port to `--set port_file=PATH` so the wrapper can read it without parsing mitmweb stdout. Only the proxy port is written — the web-UI port is pre-allocated by the wrapper so the addon doesn't need to publish it.

4. **`scripts/ndjson_to_har.py`** — post-processor. Wraps the NDJSON entries in a HAR envelope, sorts by `startedDateTime` (HAR SHOULD-order), and normalizes the `_encoding="base64"` `postData` markers back to plain `text` when the bytes are valid UTF-8 (otherwise keeps the marker — lossless but non-standard). Tolerates a truncated final NDJSON line so dumps from killed processes still produce valid HAR.

5. **`scripts/har_to_conversation.py`** — offline conversation reconstructor. Reads a HAR (`.har`/`.zst`/`.xz`/`.gz`), filters to `/v1/messages` 200-status POSTs sorted by `startedDateTime`, walks each request's `messages[]` into a shared tree (each request carries the full history, so identical prefixes merge), synthesizes the assistant reply by parsing the response SSE stream (`content_block_start`/`_delta`/`_stop` → text/thinking/tool_use blocks), and prints each block as a `# <actor> on <ts>` entry. Branches (when the user rewinds the turn) are emitted with `=========` markers, ordered by time. Filters out the haiku quota-probe POST (`{"messages":[{"role":"user","content":"quota"}],"max_tokens":1}`) so it doesn't appear as a spurious top-level branch.

### Invariants to preserve when editing

- The addons are resolved by `claude-capture` via `$DIR/mitm/...`, where `$DIR="$(dirname "$0")"`. The `mitm/` directory must stay co-located with the wrapper script.
- HAR `cache: {}` is intentional — mitmproxy is a pass-through MITM, not a caching proxy, so there's no cache state to report.
- mitmproxy must trust its own CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` (auto-created on first `mitmweb` run); `NODE_EXTRA_CA_CERTS` relies on this. If TLS validation breaks for `claude`, that's the first thing to check.
- The web UI must stay bound to `127.0.0.1` only — the token is short and meant for local convenience, not for protecting a public listener; binding elsewhere would expose live request/response traffic (including the `Authorization: Bearer` header on every claude request) to whoever guesses or sniffs the token.
- mitmweb's stdout/stderr must stay redirected away from the inherited fds: claude's TUI takes over the terminal once it starts, and any rogue log line would corrupt the display. This also hides the harmless `"Using a plaintext password"` warning emitted on every startup.
- `claude-capture` is meant to be a drop-in: don't add prompts, change exit-code semantics, or reorder so post-processing depends on claude's success — the capture must be flushed and compressed even on non-zero/crash exits.
