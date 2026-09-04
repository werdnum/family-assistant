"""The HTTP endpoint Prometheus (or vmagent) scrapes.

Served on a port of its own rather than as a route on the main app. The main
app's port is published to the internet by an Ingress that routes all of ``/``,
so a ``/metrics`` route there would publish the household's token spend, model
line-up and error rates to anyone who asked for them. A second port is routed
by nothing, which leaves the exporter reachable from inside the cluster and
nowhere else.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prometheus_client import start_http_server

if TYPE_CHECKING:
    from wsgiref.simple_server import WSGIServer

logger = logging.getLogger(__name__)

__all__ = ["start_metrics_exporter"]


def start_metrics_exporter(port: int, *, addr: str = "0.0.0.0") -> WSGIServer | None:
    """Start the metrics endpoint on *port*, returning the server it runs on.

    Returns ``None`` if the port could not be bound. A failure here is
    deliberately not fatal: losing metrics is worth strictly less than the
    assistant staying up, and the loss is visible as a scrape target that never
    comes back rather than as silence.

    The server runs on a thread of its own, outside the event loop, so a
    blocked loop still answers a scrape -- which is precisely when the answer
    is worth having.
    """
    try:
        server, _thread = start_http_server(port, addr=addr)
    except OSError:
        logger.exception("Metrics exporter could not bind %s:%d", addr, port)
        return None
    logger.info("Metrics exporter listening on http://%s:%d/metrics", addr, port)
    return server
