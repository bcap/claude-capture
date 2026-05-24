# claude-conversation-dump

A drop-in wrapper around the `claude` CLI that records every HTTP(S) request it makes to a [HAR](https://en.wikipedia.org/wiki/HAR_(file_format)) file. Useful for debugging, auditing model traffic, replaying conversations, or inspecting tool-use payloads.

## Usage

Just run `claude-captured` instead of `claude`. Any args passed are forwarded to `claude`

Eg:
```bash
./claude-captured                      # claude's regular interactive session
./claude-captured -p "summarize foo"   # any args are forwarded to claude
./claude-captured --help               # claude's own --help
```

Output (in the current working directory):

```bash
.claude-traffic-YYYYMMDD-HHMMSS.har.zst   # or .xz / .gz / .har
```

The wrapper preserves claude's exit code, so you can use it anywhere you'd use `claude` — including in scripts and pipelines.

### Configuration

All env vars are prefixed `CAPTURE_*`:

| Env var | Default | What it does |
| --- | --- | --- |
| `CAPTURE_FILE_FORMAT` | `.claude-traffic-%Y%m%d-%H%M%S.har` | Full output filename, passed verbatim to `date +<format>`. Strftime tokens expand. Don't include the compression suffix — it's appended automatically. |
| `CAPTURE_COMPRESS` | `1` | Toggle final compression. Accepts `0/1`, `true/false`, `yes/no`, `on/off`. When disabled the raw `.har` is kept. |
| `CAPTURE_MITM_FLAGS` | _(empty)_ | Extra flags forwarded to the internal `mitmdump` (word-split, no shell-quote parsing). |

```sh
CAPTURE_MITM_FLAGS='-v --set stream_large_bodies=10m' ./claude-captured
CAPTURE_FILE_FORMAT='traffic-%s.har' CAPTURE_COMPRESS=0 ./claude-captured
```

## Requirements

- `mitmproxy` (`mitmdump` on `PATH`). You can install it in several different ways:
  - `brew install mitmproxy`
  - `uv tool install mitmproxy`
  - `pipx install mitmproxy`
  - `apt install mitmproxy`
  - `pacman -S mitmproxy`
  - more at https://mitmproxy.org

Optional (auto-detected, used if present): `zstd`, `xz`, `pigz`, `gzip`. If none are installed, the HAR is left uncompressed.

If `mitmdump` is missing, `claude-captured` prints a warning and runs `claude` normally without capture — it never blocks you from using `claude`.


## Working with the captured HAR

Decompress, then open in any HAR viewer (Chrome/Firefox DevTools' Network panel → right-click → "Import HAR", or any standalone tool):

```sh
zstd -d .claude-traffic-*.har.zst    # or: xz -d / gunzip
```

The capture contains every request/response between `claude` and the API: headers, full request/response bodies (base64-encoded as per the HAR spec), and timing.

### Manual NDJSON → HAR conversion

If a run is killed before the wrapper can post-process (e.g., the host loses power), you'll be left with a `.har-entries.jsonl` file. Convert it by hand:

```sh
./ndjson_to_har.py .claude-traffic-*.har-entries.jsonl out.har
./ndjson_to_har.py - - --pretty < entries.jsonl > out.har   # stdin/stdout
```

The converter tolerates a truncated final line, so partial captures still produce a valid HAR.

## Files

- `claude-captured` — the wrapper you actually run
- `streaming_har_ndjson.py` — mitmproxy addon: writes one HAR entry per line, live
- `port_writer.py` — mitmproxy addon: publishes mitmdump's bound port
- `ndjson_to_har.py` — assembles NDJSON entries into a HAR file

## Notes and caveats

- The capture covers everything any process started inside `claude-captured` does over HTTP(S) — `claude` itself plus any child processes that inherit the proxy environment.
- Request/response bodies are stored verbatim, including auth headers. **Treat the HAR file as sensitive** — it contains your API key in the `Authorization` header on every request.
- HAR `cache: {}` is always empty by design; mitmproxy is a pass-through MITM, not a caching proxy.
- Tested on Linux; should work on macOS. Not tested on Windows.
