#!/usr/bin/env bash
# claude-capture installer / uninstaller.
#
# Install:    curl -fsSL https://raw.githubusercontent.com/bcap/claude-capture/main/install.sh | bash
# Uninstall:  curl -fsSL https://raw.githubusercontent.com/bcap/claude-capture/main/install.sh | bash -s -- --uninstall
#
# Env vars:
#   CLAUDE_CAPTURE_HOME  Install dir (default: ~/.local/share/claude-capture)
#   BIN_DIR              Symlink dir (default: ~/.local/bin)
#   CLAUDE_CAPTURE_REF   Git ref to check out (default: latest v* tag, or main if none)
#   CLAUDE_CAPTURE_REPO  Git URL (default: https://github.com/bcap/claude-capture.git)

set -euo pipefail

INSTALL_DIR="${CLAUDE_CAPTURE_HOME:-$HOME/.local/share/claude-capture}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
REPO="${CLAUDE_CAPTURE_REPO:-https://github.com/bcap/claude-capture.git}"
REF="${CLAUDE_CAPTURE_REF:-}"
SYMLINK="$BIN_DIR/claude-capture"

resolve_ref() {
    # Honor explicit override.
    if [ -n "$REF" ]; then
        return
    fi
    # Latest vX.Y tag, numerically version-sorted. Falls back to main if none.
    # --sort=-v:refname is git's version sort (numeric segments compared as
    # numbers, so v10.0 > v9.0). The grep enforces strict vX.Y to skip
    # pre-releases (vX.Y-rc1) and anything ad-hoc.
    REF="$(git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*' 2>/dev/null \
        | sed 's@.*refs/tags/@@' \
        | grep -E '^v[0-9]+\.[0-9]+$' \
        | head -1)"
    if [ -z "$REF" ]; then
        REF=main
        log "no release tags found — using branch: main"
    else
        log "resolved latest release: $REF"
    fi
}

usage() {
    cat <<EOF
Usage: install.sh [--uninstall] [--help]

  --uninstall   Remove the symlink and the install dir.
  --help        Show this message.

Env vars:
  CLAUDE_CAPTURE_HOME  Install dir       (default: ~/.local/share/claude-capture)
  BIN_DIR              Symlink dir       (default: ~/.local/bin)
  CLAUDE_CAPTURE_REF   Git ref to check out (default: latest v* tag, or main if none)
  CLAUDE_CAPTURE_REPO  Git URL           (default: https://github.com/bcap/claude-capture.git)
EOF
}

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] warning: %s\n' "$*" >&2; }
die()  { printf '[install] error: %s\n' "$*" >&2; exit 1; }

uninstall() {
    if [ -L "$SYMLINK" ]; then
        target="$(readlink "$SYMLINK")"
        case "$target" in
            "$INSTALL_DIR"/*)
                rm -f "$SYMLINK"
                log "removed symlink $SYMLINK"
                ;;
            *)
                warn "symlink $SYMLINK points to $target (not managed by us) — leaving it alone"
                ;;
        esac
    elif [ -e "$SYMLINK" ]; then
        warn "$SYMLINK exists but is not a symlink — leaving it alone"
    fi

    if [ -d "$INSTALL_DIR" ]; then
        if [ -d "$INSTALL_DIR/.git" ]; then
            rm -rf "$INSTALL_DIR"
            log "removed $INSTALL_DIR"
        else
            warn "$INSTALL_DIR exists but is not a git checkout — leaving it alone"
        fi
    fi

    log "uninstall complete"
}

install() {
    command -v git >/dev/null 2>&1 || die "git is required"

    resolve_ref
    mkdir -p "$BIN_DIR"

    if [ -d "$INSTALL_DIR/.git" ]; then
        log "updating existing checkout at $INSTALL_DIR (ref: $REF)"
        # Fetch the specific ref shallowly; --force handles moved tags. We then
        # check out FETCH_HEAD directly so this works whether $REF is a branch
        # or tag, regardless of which ref the local checkout was originally
        # created for or whether its local tags are stale.
        git -C "$INSTALL_DIR" fetch --quiet --depth 1 --force origin "$REF" \
            || die "failed to fetch ref '$REF' from $REPO"
        git -C "$INSTALL_DIR" checkout --quiet --detach --force FETCH_HEAD
    elif [ -e "$INSTALL_DIR" ]; then
        die "$INSTALL_DIR exists and is not a git checkout — refusing to clobber"
    else
        log "cloning $REPO into $INSTALL_DIR (ref: $REF)"
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --quiet --branch "$REF" --depth 1 "$REPO" "$INSTALL_DIR"
    fi

    [ -x "$INSTALL_DIR/claude-capture" ] || die "$INSTALL_DIR/claude-capture missing or not executable"

    if [ -L "$SYMLINK" ]; then
        existing="$(readlink "$SYMLINK")"
        case "$existing" in
            "$INSTALL_DIR"/claude-capture) : ;;
            *) die "$SYMLINK already points to $existing — refusing to overwrite (remove it manually or run --uninstall)" ;;
        esac
    elif [ -e "$SYMLINK" ]; then
        die "$SYMLINK exists and is not a symlink — refusing to overwrite"
    fi
    ln -sf "$INSTALL_DIR/claude-capture" "$SYMLINK"
    log "linked $SYMLINK -> $INSTALL_DIR/claude-capture"

    check_deps

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) warn "$BIN_DIR is not on your PATH. Add this to your shell rc:"
           printf '\n    export PATH="%s:$PATH"\n\n' "$BIN_DIR" >&2 ;;
    esac

    log "install complete, run claude-capture to use it"
}

check_deps() {
    if ! command -v mitmweb >/dev/null 2>&1; then
        warn "mitmweb not found on PATH. Install mitmproxy:"
        printf '    pipx install mitmproxy   # or: uv tool install mitmproxy / brew install mitmproxy / apt install mitmproxy\n' >&2
    fi

    for c in zstd xz pigz gzip; do
        if command -v "$c" >/dev/null 2>&1; then
            return 0
        fi
    done
    warn "no compressor found (zstd/xz/pigz/gzip). HAR output will be left uncompressed."
}

action="install"
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall) action="uninstall" ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
    shift
done

case "$action" in
    install)   install ;;
    uninstall) uninstall ;;
esac
