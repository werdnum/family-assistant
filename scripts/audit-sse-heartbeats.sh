#!/usr/bin/env bash
# Front-door SSE heartbeat audit (design: docs/design/ios-state-sync-improvements.md §4.7 M0).
#
# Holds an authenticated SSE stream open through a chosen network path and
# timestamps every received line, so we can verify that the ~30s server
# heartbeats actually traverse the front door and that the connection survives
# past the ~100s idle window suspected of killing streams (production cluster C).
#
# The production host resolves differently depending on where you stand
# (split-horizon DNS):
#   - LAN / home Wi-Fi:  assistant.andrewgarrett.dev -> local ingress nodes
#   - Public / cellular: assistant.andrewgarrett.dev -> Cloudflare proxy
# Both paths matter: the iOS app uses whichever the device is on. Use --path to
# force one; run the audit once per path.
#
# Auth: the stream endpoints require a real session. Supply one of:
#   FA_SSE_AUDIT_TOKEN   - API token (sent as Authorization: Bearer ...)
#   FA_SSE_AUDIT_COOKIE  - raw Cookie header value from a logged-in browser
# The diagnostics read-only token does NOT work here (it only unlocks the
# error/diagnostics endpoints).
#
# Usage:
#   scripts/audit-sse-heartbeats.sh --path lan --duration 150 [--out FILE]
#   scripts/audit-sse-heartbeats.sh --path cloudflare --duration 150
#
# Interpreting results (heartbeat interval is 30s server-side):
#   - PASS: heartbeat lines arrive at ~30s cadence for the full duration.
#   - BUFFERING: connect succeeds but no bytes arrive until disconnect.
#   - IDLE KILL: stream dies at a consistent wall-time (~60s nginx default,
#     ~100s Cloudflare) despite server heartbeats.

set -euo pipefail

HOST="assistant.andrewgarrett.dev"
DEFAULT_ENDPOINT="/api/v1/chat/activity/stream"

path_mode="default"
duration=150
endpoint="$DEFAULT_ENDPOINT"
out_file=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --path)
            path_mode="$2"
            shift 2
            ;;
        --duration)
            duration="$2"
            shift 2
            ;;
        --endpoint)
            endpoint="$2"
            shift 2
            ;;
        --out)
            out_file="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "${FA_SSE_AUDIT_TOKEN:-}" && -z "${FA_SSE_AUDIT_COOKIE:-}" ]]; then
    echo "Set FA_SSE_AUDIT_TOKEN (API token) or FA_SSE_AUDIT_COOKIE (session cookie)." >&2
    exit 2
fi

auth_args=()
if [[ -n "${FA_SSE_AUDIT_TOKEN:-}" ]]; then
    auth_args+=(-H "Authorization: Bearer ${FA_SSE_AUDIT_TOKEN}")
else
    auth_args+=(-H "Cookie: ${FA_SSE_AUDIT_COOKIE}")
fi

resolve_args=()
case "$path_mode" in
    lan | default) ;;
    cloudflare)
        public_ip="$(dig +short "$HOST" @1.1.1.1 | head -1)"
        if [[ -z "$public_ip" ]]; then
            echo "Could not resolve $HOST via public DNS." >&2
            exit 1
        fi
        resolve_args+=(--resolve "${HOST}:443:${public_ip}")
        ;;
    *)
        echo "--path must be lan, cloudflare, or default" >&2
        exit 2
        ;;
esac

if [[ -z "$out_file" ]]; then
    out_file="scratch/sse-audit-${path_mode}-$(date -u +%Y%m%dT%H%M%SZ).log"
fi
mkdir -p "$(dirname "$out_file")"

echo "Auditing https://${HOST}${endpoint} via path=${path_mode} for ${duration}s"
echo "Artifact: $out_file"

# curl exits non-zero when --max-time cuts the stream; that is the expected way
# to end a healthy run, so tolerate it and let the analyzer judge the result.
set +e
curl -sN --max-time "$duration" "${resolve_args[@]}" "${auth_args[@]}" \
    -H "Accept: text/event-stream" \
    -D - \
    "https://${HOST}${endpoint}" |
    python3 -u -c '
import sys
import time

start = time.time()
for raw in sys.stdin.buffer:
    elapsed = time.time() - start
    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    print(f"{elapsed:9.3f}s  {line}", flush=True)
' >"$out_file"
curl_status=$?
set -e

echo "curl exit status: $curl_status (28 = --max-time reached, i.e. stream survived)"
echo
python3 - "$out_file" "$duration" "$curl_status" <<'EOF'
import sys

artifact, duration = sys.argv[1], float(sys.argv[2])
curl_status = int(sys.argv[3])
events = []
with open(artifact, encoding="utf-8") as fh:
    for line in fh:
        stamp, _, rest = line.partition("s  ")
        try:
            elapsed = float(stamp)
        except ValueError:
            continue
        if rest.strip():
            events.append((elapsed, rest.strip()))

heartbeats = [e for e, text in events if text.startswith("event: heartbeat")]
last_byte = events[-1][0] if events else 0.0
gaps = [b - a for a, b in zip(heartbeats, heartbeats[1:])]

print(f"lines with content: {len(events)}")
print(f"heartbeat events:   {len(heartbeats)} at {[round(h, 1) for h in heartbeats]}")
if gaps:
    print(f"heartbeat gaps:     min {min(gaps):.1f}s max {max(gaps):.1f}s")
print(f"last byte at:       {last_byte:.1f}s of {duration:.0f}s requested")

if len(heartbeats) >= 2 and curl_status == 28:
    print("VERDICT: PASS - heartbeats traverse this path and the stream survived the full window")
elif len(heartbeats) >= 2:
    print("VERDICT: EARLY CLOSE - heartbeats traverse but the peer closed before --max-time")
elif not events:
    print("VERDICT: NO BYTES - connect failed or fully buffered; check auth/artifact")
elif not heartbeats:
    print("VERDICT: NO HEARTBEATS - bytes arrived but no heartbeat frames; buffering suspected")
else:
    print("VERDICT: EARLY DROP - stream died before requested duration; idle kill suspected")
EOF
