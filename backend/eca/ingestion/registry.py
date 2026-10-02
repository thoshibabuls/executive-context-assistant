"""Connector registry for the production worker (BACKEND_DESIGN.md §20).

Real providers are registered here from settings: Gmail (slice 1.5) when Google credentials and
the token key are configured. Tests and evaluation runners build their own registry with the
fake connectors.
"""

from __future__ import annotations

from eca.connectors import ConnectorRegistry
from eca.platform.config import Settings


def build_connector_registry(settings: Settings) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    return registry
