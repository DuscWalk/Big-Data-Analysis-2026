"""Replaceable client for the configured Chat Completions tool-call protocol."""
import json
import re
import time

import httpx


class ModelError(RuntimeError):
    def __init__(self, code, message, attempts=None):
        super().__init__(message)
        self.code = code
        self.attempts = attempts or []


def tool_schemas(specifications):
    aliases, definitions = {}, []
    for spec in specifications:
        alias = spec["name"].replace(".", "_")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) or alias in aliases:
            raise ValueError("Tool names must map to unique compatible function names.")
        aliases[alias] = spec["name"]
        definitions.append({"type": "function", "function": {
            "name": alias, "description": spec["description"],
            "parameters": spec["input_schema"],
        }})
    return aliases, definitions


class CompatibleModel:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    def payload(self, messages, definitions):
        return {
            "model": self.settings.model_name, "messages": messages, "tools": definitions,
            "tool_choice": "auto", "parallel_tool_calls": False, "stream": False,
            "temperature": 0, "max_tokens": self.settings.max_tokens,
        }

    def complete(self, payload):
        if not self.settings.configured:
            raise ModelError("MODEL_NOT_CONFIGURED", "尚未配置模型服务；任务和报告查询仍可使用。")
        try:
            with httpx.Client(timeout=self.settings.model_timeout, follow_redirects=False,
                              transport=self.transport) as client:
                # No redirects: never forward the credential to another endpoint.
                with client.stream(
                    "POST", self.settings.model_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.model_api_key.get_secret_value()},
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        raise ModelError("MODEL_HTTP_ERROR",
                            f"模型服务返回 HTTP {response.status_code}；本次模型回答未完成。")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ModelError("MODEL_RESPONSE_TOO_LARGE", "模型响应超过大小限制。")
        except httpx.TimeoutException as error:
            raise ModelError("MODEL_TIMEOUT", "模型调用超时；已受理任务仍可查询。") from error
        except httpx.HTTPError as error:
            raise ModelError("MODEL_CONNECTION_ERROR", "无法连接模型服务；已受理任务仍可查询。") from error
        try:
            response = self.settings.redact(json.loads(body))
            choice = response["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if choice.get("finish_reason") == "length":
                raise ModelError("MODEL_TRUNCATED", "模型输出被截断，未执行不完整的工具调用。")
            if not isinstance(calls, list) or len(calls) > self.settings.max_calls:
                raise ValueError("Invalid tool-call list")
            normalized, ids = [], set()
            for call in calls:
                function = call["function"]
                if call["type"] != "function" or not isinstance(function["name"], str):
                    raise ValueError("Invalid function")
                call_id = call["id"]
                if not isinstance(call_id, str) or not call_id or call_id in ids:
                    raise ValueError("Invalid call identifier")
                ids.add(call_id)
                arguments = function["arguments"]
                if not isinstance(arguments, str):
                    raise ValueError("Arguments must be serialized JSON")
                normalized.append({"id": call_id, "type": "function",
                                   "function": {"name": function["name"], "arguments": arguments}})
            content = message.get("content") or ""
            if not isinstance(content, str) or (not content.strip() and not normalized):
                raise ValueError("Empty or invalid assistant message")
            # Persist only the public reply, tool calls and usage, not provider
            # reasoning or HTTP headers.
            return {"message": {"role": "assistant", "content": content,
                                **({"tool_calls": normalized} if normalized else {})},
                    "finish_reason": choice.get("finish_reason"), "usage": response.get("usage", {})}
        except ModelError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
            raise ModelError("MODEL_INVALID_RESPONSE", "模型响应格式无效，未执行无法验证的调用。") from error


class RoutedModel:
    """At most one attempt per configured provider, before any tool dispatch.

    Every round starts with primary in auto mode. A model request may fail over;
    task execution is never replayed by this adapter.
    """
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    def payload(self, messages, definitions):
        first = self.settings.providers()[0]
        return CompatibleModel(self.settings.for_provider(first)).payload(messages, definitions)

    def complete(self, payload):
        attempts = []
        last = None
        providers = [name for name in self.settings.providers() if self.settings.provider_configured(name)]
        if not providers:
            raise ModelError("MODEL_NOT_CONFIGURED", "尚未配置模型服务；任务和报告查询仍可使用。")
        for name in providers:
            configuration = self.settings.for_provider(name)
            client = CompatibleModel(configuration, self.transport)
            actual = dict(payload, model=configuration.model_name)
            started = time.monotonic()
            try:
                response = client.complete(actual)
            except ModelError as error:
                last = error
                attempts.append({"provider": name, "model": configuration.model_name,
                                 "status": "failed", "error": error.code,
                                 "message": str(error),
                                 "duration_ms": int(1000 * (time.monotonic() - started))})
                continue
            attempts.append({"provider": name, "model": configuration.model_name,
                             "status": "completed", "duration_ms": int(1000 * (time.monotonic() - started))})
            return self.settings.redact(response | {"provider": name, "model": configuration.model_name,
                                                   "attempts": attempts})
        raise ModelError(last.code, str(last), self.settings.redact(attempts))
