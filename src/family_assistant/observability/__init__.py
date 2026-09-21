"""Observability surfaces that are not the OpenTelemetry span pipeline.

Spans answer "what did this turn do"; the Prometheus exporter here answers
"what has this deployment spent, on which profile, on which model". See
:mod:`family_assistant.observability.metrics`.
"""
