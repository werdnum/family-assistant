"""Fixtures for Home Assistant integration tests."""

import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pytest
import requests

# Import VCR compatibility patch (autouse fixture activates automatically)
from tests.integration.home_assistant.vcr_patches import (
    patch_vcr_mock_client_response,  # noqa: F401
)

# Prevent unused import warning
_ = patch_vcr_mock_client_response

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
# ast-grep-ignore: no-dict-any - Legacy code - needs structured types
def vcr_config() -> dict[str, Any]:
    """
    Override VCR config for Home Assistant tests with custom request/response filters.

    This overrides the global vcr_config to:
    - Exclude port matching (HA uses random ports in parallel tests)
    - Normalize timestamps in API paths and query parameters
    - Filter out fixture setup requests
    """
    record_mode = os.getenv("VCR_RECORD_MODE", "none")

    def before_record_request(request: Any) -> Any:  # noqa: ANN401
        """Filter out fixture setup requests and normalize timestamps."""
        # Ignore Home Assistant fixture setup requests
        if (
            request.host == "localhost"
            and request.port == 8123
            and request.path in {"/api/", "/api/onboarding/users", "/auth/token"}
        ):
            return None

        # Parse the current URI
        parsed = urlparse(request.uri)

        # Normalize timestamps in history API paths
        # Pattern: /api/history/period/2025-10-30T01:55:32+00:00
        path = parsed.path
        if "/api/history/period/" in path:
            path = re.sub(
                r"/api/history/period/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+\d{2}:\d{2}",
                "/api/history/period/{START_TIME}",
                path,
            )

        # Normalize timestamp query parameters
        query = parsed.query
        if query:
            # Parse query string into dict
            query_params = parse_qs(query, keep_blank_values=True)

            # Replace timestamp parameter values with placeholders
            if "end_time" in query_params:
                query_params["end_time"] = ["{END_TIME}"]
            if "start_time" in query_params:
                query_params["start_time"] = ["{START_TIME}"]

            # Reconstruct query string with sorted keys for consistent ordering
            query = urlencode(sorted(query_params.items()), doseq=True)

        # Reconstruct the URI with normalized path and query
        normalized_uri = urlunparse((
            parsed.scheme,
            parsed.netloc,
            path,
            parsed.params,
            query,
            parsed.fragment,
        ))

        # Update the request's URI
        request.uri = normalized_uri

        # Record the normalized request
        return request

    def before_record_response(response: Any) -> Any:  # noqa: ANN401
        """Filter out transient 404 responses during entity polling."""
        # Only filter 404 responses for the specific polling entity
        # This allows us to record 404s for actual test cases (e.g., testing nonexistent cameras)
        if response.get("status", {}).get("code") == 404:
            # Get the request dict if available (VCR.py passes it as 'request' key in some versions)
            request = response.get("request")
            if (
                request
                and request.get("uri")
                and "/api/states/input_boolean.test_switch" in request["uri"]
            ):
                return None
        # Record all other responses (including 404s for actual test cases)
        return response

    return {
        # Filter sensitive headers
        "filter_headers": [
            "authorization",
            "x-api-key",
            "api-key",
            "x-goog-api-key",
            "openai-api-key",
        ],
        # Filter sensitive query parameters
        "filter_query_parameters": ["api_key", "key"],
        # Default to "none" mode - only replay existing cassettes, don't record
        "record_mode": record_mode,
        # Match requests on these attributes
        # Note: Port is excluded to support parallel test execution with random HA ports
        # Body is excluded because URI normalization affects body serialization
        "match_on": ["method", "scheme", "host", "path", "query"],
        # Store cassettes in organized directory structure
        "cassette_library_dir": "tests/cassettes/llm",
        # Allow cassettes to be replayed multiple times
        "allow_playback_repeats": True,
        # Don't record on exceptions (avoid recording failed requests)
        "record_on_exception": False,
        # Filter out fixture setup requests and normalize timestamps
        "before_record_request": before_record_request,
        # Filter out transient 404 responses
        "before_record_response": before_record_response,
    }


def _find_free_port() -> int:
    """Find a free port by binding to port 0 and then releasing it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def _dump_ha_logs(log_file_path: Path, max_lines: int = 50) -> None:
    """Dump the last N lines of HA logs for debugging."""
    try:
        if log_file_path.exists():
            with open(log_file_path, encoding="utf-8") as f:
                lines = f.readlines()
                last_lines = lines[-max_lines:]
                logger.error(
                    "Home Assistant log (last %d lines):\n%s",
                    len(last_lines),
                    "".join(last_lines),
                )
        else:
            logger.error(f"Log file not found: {log_file_path}")
    except Exception as e:
        logger.error(f"Failed to read HA logs: {e}")


def _wait_for_ha_entity(
    base_url: str,
    access_token: str,
    entity_id: str = "input_boolean.test_switch",
    timeout_seconds: int = 30,
) -> None:
    """Wait for a specific Home Assistant entity to become available.

    This polls the entity via REST API until it becomes available, indicating
    that HA has finished loading integrations.

    Uses direct HTTP requests instead of homeassistant_api library to avoid
    issues with VCR interception during fixture setup.

    Args:
        base_url: Home Assistant base URL (http://)
        access_token: Access token for authentication
        entity_id: Entity ID to wait for
        timeout_seconds: Timeout in seconds

    Raises:
        TimeoutError: If entity doesn't become available within timeout
    """
    logger.info(f"Waiting for entity {entity_id} to become available...")
    deadline = time.time() + timeout_seconds
    last_log_time = 0
    headers = {"Authorization": f"Bearer {access_token}"}

    while time.time() < deadline:
        try:
            # Use direct HTTP request to check entity state
            response = requests.get(
                f"{base_url}/api/states/{entity_id}",
                headers=headers,
                timeout=2,
            )

            if response.status_code == 200:
                # Entity exists and is available
                logger.info(f"Entity {entity_id} is available - integrations loaded!")
                return

            # Log every 5 seconds to avoid spam
            current_time = time.time()
            if current_time - last_log_time >= 5:
                logger.info(
                    f"Entity {entity_id} not yet available (status {response.status_code}), waiting..."
                )
                last_log_time = current_time

        except requests.RequestException as e:
            # Log errors sparingly
            current_time = time.time()
            if current_time - last_log_time >= 5:
                logger.info(f"Error checking entity: {e}")
                last_log_time = current_time

        # ast-grep-ignore: no-time-sleep-in-tests - Required for polling subprocess readiness
        time.sleep(0.5)

    # Timeout reached - log all available entities for debugging
    try:
        response = requests.get(
            f"{base_url}/api/states",
            headers=headers,
            timeout=5,
        )
        if response.status_code == 200:
            all_states = response.json()
            entity_ids = [state["entity_id"] for state in all_states]
            logger.error(
                f"Timeout waiting for entity '{entity_id}'. "
                f"Found {len(entity_ids)} entities: {entity_ids[:20]}"
            )
        else:
            logger.error(
                f"Timeout waiting for entity '{entity_id}'. "
                f"Failed to retrieve entity list (status {response.status_code})"
            )
    except Exception as e:
        logger.error(f"Failed to retrieve entity list on timeout: {e}")

    raise TimeoutError(
        f"Home Assistant entity '{entity_id}' did not become available within {timeout_seconds} seconds"
    )


@pytest.fixture(scope="session")
def home_assistant_service(
    request: pytest.FixtureRequest,
) -> Generator[tuple[str, str | None]]:
    """
    Manage Home Assistant subprocess for integration testing with VCR awareness.

    This fixture checks the VCR record mode and decides whether to start Home Assistant:
    - If record_mode is "none": Skip startup (replay from cassettes) and yield the base URL
    - Otherwise: Start real Home Assistant subprocess, complete onboarding, generate token

    The fixture handles:
    - Creating a temporary config directory
    - Copying minimal configuration.yaml from fixtures
    - Starting the Home Assistant subprocess
    - Polling /api/ endpoint until ready (60s timeout)
    - Completing onboarding by creating a test user
    - Generating a long-lived access token
    - Proper cleanup and subprocess termination

    Yields:
        tuple[str, str | None]: (Base URL for Home Assistant, access token or None in replay mode)
    """
    # Get record mode from pytest-vcr flag or environment variable as fallback
    record_mode = request.config.getoption("--vcr-record", default=None)
    if record_mode is None:
        record_mode = os.getenv("VCR_RECORD_MODE", "none")

    logger.info(f"Home Assistant fixture starting with record_mode: {record_mode}")

    # If in replay mode ("none"), skip starting HA and just yield the URL
    if record_mode == "none":
        logger.info("Replay mode detected: skipping Home Assistant startup")
        # In replay mode, use a placeholder port (VCR matches without port)
        # Token is not needed (VCR replays recorded responses)
        yield ("http://localhost", None)
        return

    # Record mode: Start a real Home Assistant instance
    logger.info("Record mode detected: starting Home Assistant subprocess")

    # Find a free port for this HA instance
    port = _find_free_port()
    logger.info(f"Using port {port} for Home Assistant")

    # Create temporary directory for HA config
    temp_dir = tempfile.mkdtemp(prefix="ha_test_")
    config_dir = Path(temp_dir) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Copy configuration.yaml from fixtures
        fixture_config = (
            Path(__file__).parent.parent
            / "fixtures"
            / "home_assistant"
            / "configuration.yaml"
        )
        if not fixture_config.exists():
            raise FileNotFoundError(
                f"Home Assistant fixture config not found at {fixture_config}"
            )

        target_config = config_dir / "configuration.yaml"
        shutil.copy(fixture_config, target_config)

        # Append HTTP server configuration with custom port
        with open(target_config, "a", encoding="utf-8") as f:
            f.write(f"\n# HTTP server configuration\nhttp:\n  server_port: {port}\n")

        logger.info(f"Copied HA config to {target_config} with port {port}")

        # Validate configuration before starting
        logger.info("Validating Home Assistant configuration...")
        validate_process = subprocess.run(
            ["hass", "--script", "check_config", "-c", str(config_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if validate_process.returncode != 0:
            logger.error(
                f"Configuration validation failed:\n{validate_process.stdout}\n{validate_process.stderr}"
            )
            raise RuntimeError(
                "Home Assistant configuration is invalid. See logs above for details."
            )
        logger.info("Configuration validated successfully")

        # Start Home Assistant subprocess with log file
        log_file_path = config_dir / "home-assistant.log"
        logger.info(f"Starting Home Assistant with config dir: {config_dir}")
        process = subprocess.Popen(
            ["hass", "-c", str(config_dir), "--log-file", str(log_file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        logger.info(f"Home Assistant process started with PID: {process.pid}")
        logger.info(f"Home Assistant logs will be written to: {log_file_path}")

        # Poll the API endpoint until it's ready (60s timeout)
        # Note: API will return 401 until onboarding is complete, but that means it's running
        base_url = f"http://localhost:{port}"
        api_endpoint = f"{base_url}/api/"
        deadline = time.time() + 60

        logger.info("Waiting for Home Assistant API to start...")
        while time.time() < deadline:
            try:
                response = requests.get(api_endpoint, timeout=2)
                # Accept any HTTP response (including 401) - means server is up
                if response.status_code < 500:
                    logger.info(
                        f"Home Assistant API is responding (status: {response.status_code})"
                    )
                    break
            except (requests.RequestException, OSError) as e:
                logger.debug(f"Waiting for HA API: {e}")
                # ast-grep-ignore: no-time-sleep-in-tests - Required for polling subprocess readiness
                time.sleep(0.5)
        else:
            # Timeout reached
            process.terminate()
            raise TimeoutError(
                f"Home Assistant did not become ready within 60 seconds. "
                f"Config dir: {config_dir}"
            )

        # Complete onboarding by creating a test user
        logger.info("Completing Home Assistant onboarding...")
        onboarding_endpoint = f"{base_url}/api/onboarding/users"
        onboarding_data = {
            "name": "Test User",
            "username": "test",
            "password": "test",
            "language": "en",
            "client_id": base_url,
        }

        try:
            response = requests.post(
                onboarding_endpoint,
                json=onboarding_data,
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("Onboarding completed successfully")
                onboarding_response = response.json()
            else:
                logger.warning(
                    f"Onboarding returned {response.status_code}: {response.text}"
                )
                _dump_ha_logs(log_file_path)
                process.terminate()
                raise RuntimeError(
                    f"Onboarding failed with status {response.status_code}"
                )
        except requests.RequestException as e:
            logger.error(f"Failed to complete onboarding: {e}")
            process.terminate()
            raise

        # Authenticate to get an access token
        logger.info("Authenticating to get access token...")

        # Use the token endpoint to generate a long-lived access token
        auth_token_endpoint = f"{base_url}/auth/token"
        auth_data = {
            "grant_type": "authorization_code",
            "code": onboarding_response.get("auth_code", ""),
            "client_id": base_url,
        }

        try:
            token_response = requests.post(
                auth_token_endpoint,
                data=auth_data,
                timeout=10,
            )
            if token_response.status_code == 200:
                token_data = token_response.json()
                access_token = token_data.get("access_token")
                if not access_token:
                    process.terminate()
                    raise RuntimeError(f"No access_token in response: {token_data}")
                logger.info("Access token obtained successfully")
            else:
                process.terminate()
                raise RuntimeError(
                    f"Token request failed with status {token_response.status_code}: {token_response.text}"
                )
        except requests.RequestException as e:
            logger.error(f"Failed to get access token: {e}")
            process.terminate()
            raise

        # Wait for Home Assistant to finish loading integrations
        logger.info("Waiting for Home Assistant integrations to load...")
        try:
            _wait_for_ha_entity(base_url, access_token)
        except TimeoutError:
            _dump_ha_logs(log_file_path)
            process.terminate()
            raise

        # Yield the base URL and access token for tests to use
        yield (base_url, access_token)

    finally:
        # Cleanup: terminate subprocess and remove temp directory
        logger.info("Cleaning up Home Assistant fixture")
        try:
            if "process" in locals() and process.poll() is None:
                logger.info(f"Terminating Home Assistant process {process.pid}")
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"Forcefully killing Home Assistant process {process.pid}"
                    )
                    process.kill()
                    process.wait()
                logger.info("Home Assistant process terminated")
        except Exception as e:
            logger.error(f"Error terminating Home Assistant: {e}")

        # Remove temporary directory
        try:
            if "temp_dir" in locals():
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"Removed temporary directory: {temp_dir}")
        except Exception as e:
            logger.error(f"Error removing temporary directory: {e}")
