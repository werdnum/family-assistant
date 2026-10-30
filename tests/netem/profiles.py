"""Named network conditions for the impairment proxy.

A profile describes the link. Outages (a blackhole, a hand-over) are events a
scenario triggers at a chosen moment with ``ImpairedNetworkProxy.blackhole`` or
``reset_all_connections``, so a benchmark measures recovery from a known instant
instead of waiting for a timer.
"""

from tests.netem.proxy import Impairment

GOOD = Impairment(latency_s=0.01)

# In-flight Wi-Fi via satellite: long, variable round trips on a thin link,
# with connections that do not survive long.
AIRPLANE_WIFI = Impairment(
    latency_s=0.3,
    jitter_s=0.3,
    bandwidth_bytes_per_s=125_000,
    mean_connection_lifetime_s=90.0,
)

# A captive-portal front door: decent latency, but idle connections are cut
# and event streams are buffered rather than passed through.
HOTEL_WIFI = Impairment(
    latency_s=0.075,
    idle_cut_s=60.0,
    event_stream_buffer_bytes=8 * 1024,
)

LOSSY_CELLULAR = Impairment(
    latency_s=0.06,
    jitter_s=0.2,
    bandwidth_bytes_per_s=500_000,
    mean_connection_lifetime_s=30.0,
)

PROFILES: dict[str, Impairment] = {
    "good": GOOD,
    "airplane_wifi": AIRPLANE_WIFI,
    "hotel_wifi": HOTEL_WIFI,
    "lossy_cellular": LOSSY_CELLULAR,
}
