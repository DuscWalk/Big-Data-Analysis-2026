"""Application configuration; dotenv is parsed as data, never executed."""
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator

from ..contracts import Contract


class Settings(Contract):
    catalog: Path = Path("var/catalog.sqlite3")
    governance_config: Path = Path("configs/governance/default.json")
    model_url: str = ""
    model_name: str = ""
    model_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    backup_url: str = ""
    backup_name: str = ""
    backup_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model_provider: Literal["auto", "primary", "backup"] = "auto"
    model_timeout: float = Field(default=45, ge=1, le=180)
    max_rounds: int = Field(default=6, ge=1, le=12)
    max_calls: int = Field(default=12, ge=1, le=24)
    max_context_chars: int = Field(default=100000, ge=4096, le=250000)
    max_tokens: int = Field(default=4096, ge=64, le=8192)
    default_dataset_id: str = "ml-1m.raw"
    default_dataset_version: str | None = None
    tool_call_parser: str | None = None

    @field_validator("model_url", "backup_url")
    @classmethod
    def valid_url(cls, value):
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("MODEL_URL must be an HTTP(S) API base URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MODEL_URL must not embed credentials, a query or fragment.")
        return value.rstrip("/")

    @property
    def configured(self):
        return any(self.provider_configured(name) for name in self.providers())

    def providers(self):
        return ["primary", "backup"] if self.model_provider == "auto" else [self.model_provider]

    def provider_configured(self, name):
        if name == "backup":
            return bool(self.backup_url and self.backup_name and self.backup_api_key.get_secret_value())
        return bool(self.model_url and self.model_name and self.model_api_key.get_secret_value())

    def for_provider(self, name):
        if name == "primary":
            return self.model_copy(update={"model_provider": "primary"})
        return self.model_copy(update={"model_url": self.backup_url, "model_name": self.backup_name,
                                       "model_api_key": self.backup_api_key, "model_provider": "primary"})

    @classmethod
    def load(cls, path=Path(".env"), **overrides):
        # System environment takes priority; disable interpolation of arbitrary
        # environment values inside the user-owned configuration file.
        values = dict(dotenv_values(path, interpolate=False)) if Path(path).is_file() else {}
        values.update(os.environ)
        mapping = {
            "MODEL_URL": "model_url", "MODEL_NAME": "model_name", "MODEL_API_KEY": "model_api_key",
            "MODEL_URL_BACKUP": "backup_url", "MODEL_NAME_BACKUP": "backup_name",
            "MODEL_API_KEY_BACKUP": "backup_api_key", "MODEL_PROVIDER": "model_provider",
            "MODEL_TIMEOUT_SECONDS": "model_timeout", "AGENT_MAX_ROUNDS": "max_rounds",
            "AGENT_MAX_CALLS": "max_calls", "AGENT_MAX_CONTEXT_CHARS": "max_context_chars",
            "MODEL_MAX_TOKENS": "max_tokens", "DEFAULT_DATASET_VERSION": "default_dataset_version",
            "DEFAULT_DATASET_ID": "default_dataset_id", "TOOL_CALL_PARSER": "tool_call_parser",
        }
        arguments = {field: values[key] for key, field in mapping.items() if values.get(key)}
        return cls.model_validate(arguments | overrides)

    def redact(self, value):
        """Remove the configured secret even if a provider echoes it."""
        if isinstance(value, str):
            for secret in (self.model_api_key.get_secret_value(), self.backup_api_key.get_secret_value()):
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value
