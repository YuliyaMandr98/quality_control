"""Integration framework and base client."""

from abc import ABC, abstractmethod
from typing import Any, Optional

from packages.common import IntegrationConnectionStatus, IntegrationType, get_logger

logger = get_logger(__name__)


class IntegrationClient(ABC):
    """Base class for all integration providers."""

    provider_type: IntegrationType

    def __init__(self, config: dict[str, Any]):
        """Initialize with configuration."""
        self.config = config

    @abstractmethod
    async def test_connection(self) -> tuple[bool, Optional[str]]:
        """Test connection to provider. Returns (success, error_message)."""
        pass

    @abstractmethod
    async def get_health_status(self) -> IntegrationConnectionStatus:
        """Get current health status."""
        pass

    def _mask_config(self) -> dict[str, Any]:
        """Return config with secrets masked for logging."""
        masked = self.config.copy()
        for key in ["token", "pat", "api_key", "password"]:
            if key in masked and masked[key]:
                masked[key] = f"***{masked[key][-4:]}" if len(masked[key]) > 4 else "***"
        return masked


class IntegrationRegistry:
    """Factory and registry for integration clients."""

    def __init__(self):
        self._clients: dict[IntegrationType, type[IntegrationClient]] = {}

    def register(self, provider_type: IntegrationType, client_class: type[IntegrationClient]) -> None:
        """Register an integration client."""
        self._clients[provider_type] = client_class
        logger.info(f"Registered integration client: {provider_type.value}")

    def get_client(
        self, provider_type: IntegrationType, config: dict[str, Any]
    ) -> IntegrationClient:
        """Get a client instance for a provider."""
        if provider_type not in self._clients:
            raise ValueError(f"Unknown integration provider: {provider_type.value}")
        return self._clients[provider_type](config)

    def list_providers(self) -> list[IntegrationType]:
        """List all registered providers."""
        return list(self._clients.keys())


# Global registry
integration_registry = IntegrationRegistry()
