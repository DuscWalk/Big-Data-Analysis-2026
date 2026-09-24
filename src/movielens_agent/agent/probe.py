"""Explicit, credential-safe checks for an existing compatible model service."""
import json

import httpx

from .model import ModelError, RoutedModel


def list_models(settings):
    results = []
    for name in settings.providers():
        config = settings.for_provider(name)
        item = {"provider": name}
        if not config.model_url or not config.model_api_key.get_secret_value():
            results.append(item | {"error": "MODEL_NOT_CONFIGURED"})
            continue
        try:
            with httpx.Client(timeout=config.model_timeout, follow_redirects=False) as client:
                with client.stream("GET", config.model_url + "/models", headers={
                    "Authorization": "Bearer " + config.model_api_key.get_secret_value()}) as response:
                    item["http_status"] = response.status_code
                    body = bytearray()
                    if response.status_code == 200:
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > 2_000_000:
                                raise ValueError("Response too large")
                        values = json.loads(body)["data"]
                        item["models"] = [value["id"] for value in values if isinstance(value.get("id"), str)]
        except Exception as error:
            item["error"] = type(error).__name__
        results.append(item)
    return settings.redact({"providers": results})


def tool_probe(settings):
    model = RoutedModel(settings)
    messages = [{"role": "user", "content": "Call echo with text ready."}]
    definitions = [{"type": "function", "function": {
        "name": "echo", "description": "Return the input text.", "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False}}}]
    payload = model.payload(messages, definitions)
    payload["tool_choice"] = {"type": "function", "function": {"name": "echo"}}
    try:
        response = model.complete(payload)
        calls = response["message"].get("tool_calls", [])
        valid = len(calls) == 1 and calls[0]["function"]["name"] == "echo"
        valid = valid and json.loads(calls[0]["function"]["arguments"]) == {"text": "ready"}
        return {"status": "completed" if valid else "failed", "native_tool_call": valid,
                "provider": response["provider"], "model": response["model"],
                "attempts": response["attempts"]}
    except (ValueError, KeyError, TypeError):
        return {"status": "failed", "error": "INVALID_PROBE_CALL"}
    except ModelError as error:
        return {"status": "failed", "error": error.code, "message": str(error), "attempts": error.attempts}
