# M0 front-door SSE heartbeat audit — findings so far

Companion artifact for the M0 milestone of
[docs/design/ios-state-sync-improvements.md](../design/ios-state-sync-improvements.md) (§4.7, §7.2).
Records what was established on 2026-07-21 and what still needs an authenticated run.

## Network topology (established)

`assistant.andrewgarrett.dev` is **split-horizon**:

| Vantage point        | Resolves to                      | Path                          |
| -------------------- | -------------------------------- | ----------------------------- |
| LAN (home Wi-Fi)     | 6 × `192.168.124.2xx` A records  | local ingress nodes → uvicorn |
| Public DNS (1.1.1.1) | `104.21.12.120`, `172.67.132.36` | Cloudflare proxy → origin     |

So the iOS app traverses **two different front doors** depending on whether the device is on home
Wi-Fi or cellular. Both must be audited; a fix to one does not cover the other. On the LAN path the
TLS termination presents a Let's Encrypt `*.andrewgarrett.dev` cert and passes uvicorn's own
response headers through unmodified (HTTP/1.1, `server: uvicorn`, no h2).

Cloudflare's documented behavior for proxied connections includes a ~100 s idle cutoff between
bytes, which matches the design's production cluster C (`streamDrop` after ~96 s idle during quiet
tool calls) — still correlation, not yet proof; the authenticated run below settles it.

## Origin behavior (verified locally, 2026-07-21)

Backend booted from this commit with auth disabled, activity stream held open with
`curl -N | timestamper`:

```
  29.996s  event: heartbeat
  29.996s  data: {}
  59.997s  event: heartbeat
  59.998s  data: {}
```

- Heartbeats are emitted at a clean 30 s cadence (origin side is healthy).
- Response headers: `x-accel-buffering: no`, `cache-control: no-cache`,
  `content-type: text/event-stream`, chunked, no content-length — correct for SSE.
- **The activity stream sends no bytes at all until the first heartbeat at t=30 s.** Response
  headers arrive immediately, but the body is silent for 30 s on an idle account. Two consequences:
  1. A buffering front door has nothing to flush for 30 s, so "connect succeeded but no events" is
     indistinguishable from buffering until t=30 s.
  2. The iOS client infers "connected" from response headers alone; a front door that holds headers
     until first body byte would delay the connect signal by up to 30 s.

## Authenticated front-door run (blocked — needs a credential)

The stream endpoints require a real session. The diagnostics read-only token was tested and does
**not** unlock them (401, as designed). To complete the audit, run, with either an API token
(`FA_SSE_AUDIT_TOKEN`) or a logged-in browser's cookie header (`FA_SSE_AUDIT_COOKIE`):

```bash
scripts/audit-sse-heartbeats.sh --path lan --duration 150
scripts/audit-sse-heartbeats.sh --path cloudflare --duration 150
```

The script timestamps every received line, writes the artifact to `scratch/`, and prints a PASS / NO
HEARTBEATS (buffering) / EARLY DROP (idle kill) verdict per path. Success criterion (design §7.2):
heartbeats at ~30 s cadence for the full 150 s on **both** paths. Expected failure signatures:
Cloudflare idle kill near ~100 s; LAN ingress (nginx-class) idle kill near ~60 s if
`proxy_read_timeout` is default.

Record the two artifacts (or the ingress fix that makes them pass) in this file when run.

## Deployment posture (2026-07-21)

Front-door timeout bumps are optional and evidence-driven, not a prerequisite. The origin already
emits heartbeats at a 30 s cadence, which sits comfortably inside even Cloudflare's ~100 s idle
window whenever those heartbeats actually traverse the front door — so an idle kill only happens
when a proxy buffers or drops the stream, not because the cadence is too slow. And after M1–M3 the
cost of an idle kill is bounded: it is one silent reconnect (resume from the last applied seq, no
lost turn, no user-visible error). So the decision on whether any ingress change is worth making is
deferred to data: the authenticated audit runs above plus production telemetry (the
`Chat.streamDrop` / `Chat.liveStreamDrop` breadcrumbs) decide whether a front-door timeout bump buys
anything measurable.
