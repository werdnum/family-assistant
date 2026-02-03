# Asterisk Live / Gemini Live: Investigation Report

This document records a real-world debugging session where Asterisk → Gemini Live calls worked for
the **first** user turn and then went silent. It captures what was observed, the root causes, the
fixes, and what we learned so future readers can avoid repeating the same traps.

## Summary (What was broken)

- **Symptom A:** First user utterance got a response, subsequent utterances were ignored.
- **Symptom B:** Later, the WebSocket handshake to `/api/asterisk/live` started returning **403**.
- **Secondary:** Audio sounded choppy even on a local SIP network.

## Root Causes

1. **Gemini Live receive loop only returned one turn**

   - The Google Live SDK’s `AsyncSession.receive()` yields until it hits `turn_complete` and then
     returns. Our handler called `receive()` once, so it never consumed subsequent turns.
   - Fix: wrap `receive()` in an outer loop and re-enter it for each turn.

2. **FastAPI dependency injection broke due to type-only import**

   - `LiveAudioClient` was moved into a `TYPE_CHECKING` block. With
     `from __future__ import annotations`, FastAPI could not resolve the annotation at runtime and
     treated `client` as a required **query parameter**. Missing it caused an automatic **403**
     before the handler ran.
   - Fix: keep `LiveAudioClient` imported at runtime with a comment explaining why.

3. **Choppy output audio caused by small frames and unpaced send**

   - Asterisk sends `optimal_frame_size` (often 20ms). Resampled audio was forwarded in small frames
     without pacing, leading to jitter.
   - Fix: enforce a minimum 40ms send frame size, add pacing backoff when the buffer drains, and
     write pacing stats to the packet trace to confirm behavior.

## Fixes Implemented

- **Multi-turn receive loop:** `_iter_gemini_messages()` now re-enters `session.receive()` for each
  subsequent turn instead of stopping after the first `turn_complete`.
- **Runtime dependency injection:** `LiveAudioClient` is imported at runtime so FastAPI resolves the
  dependency correctly.
- **Tool calls:** Gemini tool calls are executed and replied to (previously ignored, causing
  stalls).
- **Partial audio ducking:** While assistant audio plays, user audio is attenuated rather than muted
  to reduce echo without losing barge-in.
- **Send pacing:** Outbound audio uses ≥40ms frames with brief sleep pacing when buffer underflows.
- **Debug logging:** `ASTERISK_LIVE_DEBUG` now defaults to **off**. Packet traces go to
  `/var/log/family-assistant/asterisk-live/<session>/packet_trace.jsonl` when enabled.

## Validation (What proved the fixes)

- **Direct WebSocket handshake** to `ws://devcontainer-backend-1:8000/api/asterisk/live?...`
  succeeded after restoring runtime dependency injection.
- **Multi-turn replay** (previously only first reply) now yields multiple assistant turns in a
  single Live session.
- **Packet traces** show consistent pacing metrics (`kind: "pacing"`) with larger frame sizes.

## What We Learned

- **SDKs can look “streaming” while still being turn-bounded.** Read the receive loop carefully.
- **FastAPI dependencies are runtime, not just typing.** Moving annotations into `TYPE_CHECKING` can
  silently change how dependencies are resolved and cause 403s that never reach your handler.
- **Telephony streams need explicit pacing.** Sending raw frames as fast as possible sounds choppy
  even on a LAN.
- **Local VAD was a red herring.** The multi-turn silence issue was not caused by missing VAD
  boundaries; it was caused by the receive loop exiting after a single turn.

## Debugging Checklist (if this regresses)

1. **WebSocket 403?**

   - Verify the handler is reached (log at the top of `asterisk_live_endpoint`).
   - Confirm `LiveAudioClient` is a runtime import, not `TYPE_CHECKING`.
   - Confirm `ASTERISK_SECRET_TOKEN` and `ASTERISK_ALLOWED_EXTENSIONS` match Asterisk config.

2. **Only first reply?**

   - Confirm `_iter_gemini_messages()` wraps `session.receive()` in an outer loop.

3. **Choppy audio?**

   - Check `packet_trace.jsonl` for `pacing` entries.
   - Ensure `send_frame_size` ≥ 40ms of audio.
   - Confirm resampler output size and buffer depth are stable.

## Notes on Repro Assets

During the investigation we used recorded sessions under:

```
/var/log/family-assistant/asterisk-live/<SESSION>/
```

Those PCM files were converted to WAV for offline transcription and replay testing. The ad‑hoc
replay script used during investigation has been removed after the fix; if you ever need to re-run
those experiments, re-create a small replay script that:

- streams `gemini_in.pcm` (16k PCM) into the Live API
- re-enters `session.receive()` for multiple turns
- logs model transcripts and audio parts

That approach is sufficient to validate multi-turn behavior without a live call.
