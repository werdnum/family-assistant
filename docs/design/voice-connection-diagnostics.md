# Native voice connection diagnostics

## Approach

iPhone and watch voice attempts share a diagnostic identity from permission through token retrieval,
audio activation, socket setup, acknowledgement, and termination. Timestamped metadata goes through
the existing error reporter: lifecycle observations use the telemetry lane; failures use the error
lane. A startup deadline covers token retrieval through setup acknowledgement, but not the user's
microphone permission decision. API version, tools and voice protocol remain unchanged.

Transport adapters observe their native connection callbacks. iOS records WebSocket open/close, HTTP
response status when available, and structured error codes. watchOS records Network framework states
and received close frames. Setup size and function count are numbers, never setup contents.

## Privacy and deliberate limits

Error descriptions and arbitrary server close reasons can contain authenticated URLs or echoed
private setup data. Voice diagnostics use allowlisted error domains, numeric codes and bounded
underlying-error depth. Close reasons are classified into fixed categories, retaining code and byte
count but not raw text. Unknown reasons are explicitly unclassified. This may require another
targeted investigation when Google returns an unfamiliar reason; it does not justify uploading
arbitrary private data. No audio, transcripts, tool names/arguments or credentials are collected.

Delivery remains best-effort through the existing authenticated reporter and its bounded disk queue.
Voice entry retries queued reports in addition to the existing launch retry. Backend intake allows
60 reports per minute per authenticated user, shared with chat telemetry and paired devices; a burst
can therefore delay delivery. We have not established why the observed build 74 failure report was
missing. No new delivery guarantee, backend exemption, or entitlement is introduced.

## Verification

Tests cover stage ordering and attempt correlation, structured failures, privacy exclusions, close
reason classification, startup timeout and cancellation after setup acknowledgement. Build both iOS
and watch targets. A TestFlight reproduction is still required to capture the physical-device
failure; simulator unit tests cannot establish its cause.
