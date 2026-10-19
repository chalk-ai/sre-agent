"""Resolve non-secret routing settings with the SDK's ambient Chalk login."""

from __future__ import annotations

import os


def load_chalk_env() -> None:
    from chalkcompute import ConnectClient

    client = ConnectClient()
    try:
        client._ensure_auth()
        os.environ["CHALK_API_SERVER"] = client._api_server
        os.environ["CHALK_ENVIRONMENT_ID"] = client._env_id
    finally:
        client.close()
