#!/bin/sh
set -eu

marker="${WIREPROXY_RECONNECT_MARKER:-}"
poll_seconds="${WIREPROXY_RECONNECT_POLL_SECONDS:-2}"

# Non-WARP containers keep the original direct-exec behavior.
if [ -z "$marker" ]; then
    exec /usr/local/bin/wireproxy-bin "$@"
fi

last_token=""
if [ -f "$marker" ]; then
    last_token="$(sed -n '1p' "$marker" 2>/dev/null || true)"
fi

child_pid=""
stopping=0

stop_child() {
    stopping=1
    if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then
        kill -TERM "$child_pid" 2>/dev/null || true
        wait "$child_pid" 2>/dev/null || true
    fi
    exit 0
}

trap stop_child INT TERM

while :; do
    /usr/local/bin/wireproxy-bin "$@" &
    child_pid=$!
    reconnect=0

    while kill -0 "$child_pid" 2>/dev/null; do
        token=""
        if [ -f "$marker" ]; then
            token="$(sed -n '1p' "$marker" 2>/dev/null || true)"
        fi
        if [ -n "$token" ] && [ "$token" != "$last_token" ]; then
            last_token="$token"
            reconnect=1
            echo "[wireproxy-reconnect] marker changed; reconnecting tunnel"
            kill -TERM "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
            child_pid=""
            break
        fi
        sleep "$poll_seconds" &
        wait $! 2>/dev/null || true
    done

    if [ "$stopping" -eq 1 ]; then
        exit 0
    fi
    if [ "$reconnect" -eq 1 ]; then
        sleep 1
        continue
    fi

    wait "$child_pid"
    exit $?
done
