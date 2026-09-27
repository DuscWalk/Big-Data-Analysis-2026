"""Local model preferences: redacted views, revision checks and atomic storage."""
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import Field, SecretStr, ValidationError, field_validator

from ..contracts import Contract
from .settings import Settings

MODEL_FIELDS = ("model_url", "model_name", "model_api_key", "backup_url", "backup_name",
                "backup_api_key", "model_provider", "model_timeout", "max_tokens")


class SettingsConflict(ValueError):
    pass


class ProviderEdit(Contract):
    url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=200)
    api_key: SecretStr | None = None
    clear_key: bool = False

    @field_validator("url", "model")
    @classmethod
    def clean_text(cls, value):
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is None or not value.get_secret_value().strip():
            return None
        key = value.get_secret_value().strip()
        if len(key) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise ValueError("API Key 格式无效。")
        return SecretStr(key)


class ModelEdit(Contract):
    revision: str = Field(min_length=1, max_length=64)
    provider: Literal["auto", "primary", "backup"]
    timeout_seconds: float = Field(ge=1, le=180, allow_inf_nan=False)
    max_tokens: int = Field(ge=64, le=8192)
    primary: ProviderEdit
    backup: ProviderEdit


class ModelCheck(Contract):
    provider: Literal["primary", "backup"]
    configuration: ModelEdit


class ModelPreferences:
    def __init__(self, settings):
        self.path = Path(settings.catalog).resolve().with_suffix(".models.json")
        self.lock = RLock()
        self.settings, self.revision = settings, uuid4().hex
        self.saved = self.path.is_file()
        if self.saved:
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                values = saved["settings"]
                if (saved["schema_version"] != 1 or set(values) != set(MODEL_FIELDS)
                        or not isinstance(saved["revision"], str) or len(saved["revision"]) != 32):
                    raise ValueError("Invalid saved settings")
                self.settings = Settings.model_validate(settings.model_dump() | values)
                self.revision = saved["revision"]
                self.path.chmod(0o600)
            except (OSError, ValueError, TypeError, KeyError):
                raise RuntimeError("无法读取已保存的模型设置，请检查本地配置文件。") from None

    def snapshot(self):
        with self.lock:
            return self.settings

    def public(self):
        with self.lock:
            settings = self.settings
            def provider(name):
                item = settings.for_provider(name)
                return {"url": item.model_url, "model": item.model_name,
                        "key_configured": bool(item.model_api_key.get_secret_value()),
                        "configured": settings.provider_configured(name)}
            return settings.redact({"revision": self.revision, "saved": self.saved,
                "provider": settings.model_provider, "timeout_seconds": settings.model_timeout,
                "max_tokens": settings.max_tokens, "primary": provider("primary"), "backup": provider("backup")})

    def prepare(self, edit):
        with self.lock:
            if edit.revision != self.revision:
                raise SettingsConflict("模型设置已在其他页面更新，请重新打开设置后再操作。")
            values = self.settings.model_dump()
            for name, prefix in (("primary", "model"), ("backup", "backup")):
                entry = getattr(edit, name)
                previous = self.settings.for_provider(name)
                if entry.clear_key and entry.api_key is not None:
                    raise ValueError("不能同时填写和清除同一服务的密钥。")
                # Empty URL disables the endpoint; it cannot receive a retained
                # key. A later nonempty replacement still needs an explicit key.
                if (entry.url and entry.url.rstrip("/") != previous.model_url and entry.api_key is None
                        and not entry.clear_key and previous.model_api_key.get_secret_value()):
                    raise ValueError("更换服务地址时，请重新填写 API Key 或勾选清除已保存密钥。")
                key = SecretStr("") if entry.clear_key else entry.api_key or previous.model_api_key
                values.update({prefix + "_url": entry.url, prefix + "_name": entry.model,
                               prefix + "_api_key": key})
            values.update(model_provider=edit.provider, model_timeout=edit.timeout_seconds,
                          max_tokens=edit.max_tokens)
            try:
                return Settings.model_validate(values)
            except ValidationError:
                raise ValueError("模型设置无效；请检查服务地址和数值范围。") from None

    def save(self, edit):
        with self.lock:
            candidate = self.prepare(edit)
            revision = uuid4().hex
            values = {key: getattr(candidate, key) for key in MODEL_FIELDS}
            for key in ("model_api_key", "backup_api_key"):
                values[key] = values[key].get_secret_value()
            data = json.dumps({"schema_version": 1, "revision": revision, "settings": values},
                              ensure_ascii=False, indent=2) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".model-settings-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.settings, self.revision, self.saved = candidate, revision, True
            return candidate
