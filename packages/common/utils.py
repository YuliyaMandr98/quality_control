"""Shared utilities: structured logging and secret encryption."""

import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from cryptography.fernet import Fernet


class StructuredLogger:
    """JSON-structured logging for backend, readable format for development."""

    def __init__(self, name: str, json_mode: bool = False):
        self.logger = logging.getLogger(name)
        self.json_mode = json_mode

    def _format_message(
        self,
        level: str,
        message: str,
        correlation_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        if self.json_mode:
            payload = {
                "timestamp": datetime.utcnow().isoformat(),
                "level": level,
                "message": message,
                "correlation_id": correlation_id or "unknown",
            }
            if extra:
                payload.update(extra)
            return json.dumps(payload)
        else:
            msg = f"[{level}] {message}"
            if correlation_id:
                msg = f"{msg} (correlation_id={correlation_id})"
            if extra:
                msg = f"{msg} {extra}"
            return msg

    def info(
        self,
        message: str,
        correlation_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self.logger.info(self._format_message("INFO", message, correlation_id, extra))

    def error(
        self,
        message: str,
        correlation_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self.logger.error(self._format_message("ERROR", message, correlation_id, extra))

    def warning(
        self,
        message: str,
        correlation_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self.logger.warning(self._format_message("WARNING", message, correlation_id, extra))

    def debug(
        self,
        message: str,
        correlation_id: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        self.logger.debug(self._format_message("DEBUG", message, correlation_id, extra))


def get_logger(name: str, json_mode: bool = False) -> StructuredLogger:
    """Get a structured logger instance."""
    return StructuredLogger(name, json_mode)


class SecretEncryption:
    """Encrypt/decrypt secrets at rest."""

    def __init__(self, key: str):
        """Initialize with encryption key (base64 or will be generated)."""
        if not key or key == "dev-encryption-key-change-in-production":
            # Generate a dev-safe key for local testing
            self.cipher = Fernet(Fernet.generate_key())
        else:
            try:
                self.cipher = Fernet(key.encode() if isinstance(key, str) else key)
            except Exception:
                self.cipher = Fernet(Fernet.generate_key())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext secret."""
        return self.cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt an encrypted secret."""
        try:
            return self.cipher.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ""

    def mask_secret(self, secret: str, show_chars: int = 4) -> str:
        """Mask a secret for logging, showing only last N characters."""
        if len(secret) <= show_chars:
            return "*" * len(secret)
        return "*" * (len(secret) - show_chars) + secret[-show_chars:]


def generate_correlation_id() -> str:
    """Generate a correlation ID for request/job tracking."""
    return str(uuid4())


def mask_dict_secrets(data: dict[str, Any], secret_keys: list[str]) -> dict[str, Any]:
    """Mask sensitive keys in a dictionary for logging."""
    encrypted = SecretEncryption("")
    masked = data.copy()
    for key in secret_keys:
        if key in masked and masked[key]:
            masked[key] = encrypted.mask_secret(str(masked[key]))
    return masked
