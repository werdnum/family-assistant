"""Disable auth for all functional tests to prevent OIDC redirects."""

import os

# IMPORTANT: Disable auth BEFORE importing any web modules
# This must happen at the top level of the functional test package
# to ensure it takes effect before any web modules are imported
os.environ["OIDC_CLIENT_ID"] = ""
os.environ["OIDC_CLIENT_SECRET"] = ""
os.environ["OIDC_DISCOVERY_URL"] = ""
os.environ["SESSION_SECRET_KEY"] = ""

# This file ensures auth is disabled for ALL functional tests,
# not just the web tests. This prevents issues when tests run
# in parallel and import web modules in different orders.
