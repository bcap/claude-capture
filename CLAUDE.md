# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tiny tooling repo that wraps the `claude` CLI with an mitmproxy-based HTTP(S) capture so every conversation's network traffic is recorded as a HAR file. No build system, no tests, no package manifest — just three Python scripts and a Bash wrapper.

## Commands

- Run claude with capture: `./claude-captured [args forwarded to claude]`
  - Optional env vars (all `CAPTURE_*`-prefixed):
    - `CAPTURE_MITM_FLAGS='...'` — extra flags forwarded to `mitmdump` (word-split, no shell-quote parsing).
    - `CAPTURE_FILE_FORMAT='...'` — full output filename, passed verbatim to `date +<format>` so strftime tokens expand. Default `.claude-traffic-%Y%m%d-%H%M%S.har`. Do not include the compression suffix; it is appended automatically.
    - `CAPTURE_COMPRESS=0|1|true|false|yes|no|on|off` — toggle compression. Default `1` (enabled). When disabled the raw `.har` is left in place.
  - Produces `<CAPTURE_FILE_FORMAT>[.zst|.xz|.gz]` in the current working directory.
  - Acts as a drop-in `claude` replacement: forwards args via `"$@"`, propagates claude's exit code, and `exec`s claude directly if `mitmdump` is missing.
- Convert a saved NDJSON capture to HAR manually: `./scripts/ndjson_to_har.py INPUT.ndjson OUTPUT.har` (use `-` for stdin/stdout; `--pretty` for indented; `--no-sort` to stream without holding entries in memory).

There are no tests or linters configured. Don't add a CI/test scaffold unless explicitly asked.

## Architecture

The four pieces have a strict pipeline relationship — changing one usually means touching another:

1. **`claude-captured`** — orchestrator. Starts `mitmdump -p 0` (ephemeral port) loading the two addons, polls a temp port-file (≤10s) for the bound port, exports `HTTPS_PROXY` / `HTTP_PROXY` / `NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem`, runs `claude`, then shuts mitmdump down via `graceful_stop` (SIGTERM → 5s wait → SIGKILL → `wait` to reap), runs `scripts/ndjson_to_har.py`, and compresses with the first available of zstd → xz → pigz → gzip.

2. **`mitm/streaming_har_ndjson.py`** — mitmproxy addon. On every `response`/`error` hook, appends one fully-formed HAR `log.entries[]` object as a single line to `--set har_ndjson=PATH`, then fsyncs. This is the durability contract: a SIGKILL mid-session loses at most the in-flight flow, never the file. Bodies are stored base64 in `response.content` (standard HAR) and, for non-trivial request bodies, in a non-standard `request.postData._encoding="base64"` marker.

3. **`mitm/port_writer.py`** — mitmproxy addon. In `running()` (fires once the proxy is bound), atomically writes the listen port to `--set port_file=PATH` so the wrapper can read it without parsing mitmdump stdout.

4. **`scripts/ndjson_to_har.py`** — post-processor. Wraps the NDJSON entries in a HAR envelope, sorts by `startedDateTime` (HAR SHOULD-order), and normalizes the `_encoding="base64"` `postData` markers back to plain `text` when the bytes are valid UTF-8 (otherwise keeps the marker — lossless but non-standard). Tolerates a truncated final NDJSON line so dumps from killed processes still produce valid HAR.

### Invariants to preserve when editing

- The addons are resolved by `claude-captured` via `$DIR/mitm/...`, where `$DIR="$(dirname "$0")"`. The `mitm/` directory must stay co-located with the wrapper script.
- HAR `cache: {}` is intentional — mitmproxy is a pass-through MITM, not a caching proxy, so there's no cache state to report.
- mitmproxy must trust its own CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` (auto-created on first `mitmdump` run); `NODE_EXTRA_CA_CERTS` relies on this. If TLS validation breaks for `claude`, that's the first thing to check.
- `claude-captured` is meant to be a drop-in: don't add prompts, change exit-code semantics, or reorder so post-processing depends on claude's success — the capture must be flushed and compressed even on non-zero/crash exits.
