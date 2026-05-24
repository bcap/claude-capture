# claude-conversation-dump

A drop-in wrapper around the `claude` CLI that records every HTTP(S) request it makes to a [HAR](https://en.wikipedia.org/wiki/HAR_(file_format)) file. Useful for debugging, auditing model traffic, replaying conversations, or inspecting tool-use payloads.

Run claude through `claude.sh` instead of `claude`. When it exits you get a compressed HAR alongside your normal claude output.

## How it works

`claude.sh` starts [mitmproxy](https://mitmproxy.org/) (`mitmdump`) on a random local port, runs `claude` with `HTTPS_PROXY` / `HTTP_PROXY` pointed at it and `NODE_EXTRA_CA_CERTS` set so TLS validates against mitmproxy's CA. Two small mitmproxy addons stream each completed flow to disk as JSON-lines (so nothing is lost if the process is killed). On exit, the lines are assembled into a HAR and compressed with the best available compressor.

## Requirements

- `mitmproxy` (`mitmdump` on `PATH`) — installation options:
  - `brew install mitmproxy`
  - `uv tool install mitmproxy`
  - `pipx install mitmproxy`
  - `apt install mitmproxy`
  - `pacman -S mitmproxy`
  - more at https://mitmproxy.org
- `python3` (used by the post-processor; mitmproxy already requires it)
- The `claude` CLI on `PATH`
- A mitmproxy CA at `~/.mitmproxy/mitmproxy-ca-cert.pem` — created automatically the first time `mitmdump` runs

Optional (auto-detected, used if present): `zstd`, `xz`, `pigz`, `gzip`. If none are installed, the HAR is left uncompressed.

If `mitmdump` is missing, `claude.sh` prints a warning and runs `claude` normally without capture — it never blocks you from using `claude`.

## Usage

```sh
./claude.sh                      # interactive session
./claude.sh -p "summarize foo"   # any args are forwarded to claude
./claude.sh --help               # claude's own --help
```

Output (in the current working directory):

```
.claude-traffic-YYYYMMDD-HHMMSS.har.zst   # or .xz / .gz / .har
```

The wrapper preserves claude's exit code, so you can use it anywhere you'd use `claude` — including in scripts and pipelines.

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

- `claude.sh` — the wrapper you actually run
- `streaming_har_ndjson.py` — mitmproxy addon: writes one HAR entry per line, live
- `port_writer.py` — mitmproxy addon: publishes mitmdump's bound port
- `ndjson_to_har.py` — assembles NDJSON entries into a HAR file

## Notes and caveats

- The capture covers everything any process started inside `claude.sh` does over HTTP(S) — `claude` itself plus any child processes that inherit the proxy environment.
- Request/response bodies are stored verbatim, including auth headers. **Treat the HAR file as sensitive** — it contains your API key in the `Authorization` header on every request.
- HAR `cache: {}` is always empty by design; mitmproxy is a pass-through MITM, not a caching proxy.
- Tested on Linux; should work on macOS. Not tested on Windows.
