"""Throwaway entities a test creates directly in a live Home Assistant.

Creating and removing them is setup, so those requests are kept out of
cassettes; see ``before_record_request`` in this package's conftest.
"""

BULK_ENTITY_ID_PREFIX = "sensor.fa_bulk_"
