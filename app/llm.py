"""One interface over Ollama (local), Gemini and OpenAI (or any OpenAI-compatible endpoint)."""
import json
import re

import httpx

DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


class LLMError(Exception):
    pass


def _base(cfg: dict) -> str:
    return (cfg.get("base_url") or DEFAULT_URLS[cfg["provider"]]).rstrip("/")


def _headers(cfg: dict) -> dict:
    key = cfg.get("api_key") or ""
    if cfg["provider"] != "ollama" and not key:
        raise LLMError(f"An API key is required for {cfg['provider']}.")
    return {"Authorization": f"Bearer {key}"} if key else {}


def list_models(cfg: dict) -> list[str]:
    provider = cfg.get("provider", "ollama")
    cfg = {**cfg, "provider": provider}
    try:
        if provider == "ollama":
            r = httpx.get(_base(cfg) + "/api/tags", timeout=10)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        r = httpx.get(_base(cfg) + "/models", headers=_headers(cfg), timeout=20)
        r.raise_for_status()
        names = [m["id"].removeprefix("models/") for m in r.json().get("data", [])]
        return sorted(names)
    except httpx.HTTPError as e:
        raise LLMError(f"Could not list models: {e}") from e


def _call(cfg: dict, messages: list[dict], timeout: float) -> str:
    provider = cfg["provider"]
    model = cfg.get("model")
    if not model:
        raise LLMError("No model selected.")
    try:
        if provider == "ollama":
            r = httpx.post(
                _base(cfg) + "/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "options": {"num_ctx": 16384, "temperature": 0.3},
                },
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["message"]["content"]
        r = httpx.post(
            _base(cfg) + "/chat/completions",
            headers=_headers(cfg),
            json={"model": model, "messages": messages, "response_format": {"type": "json_object"}},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        raise LLMError(f"{provider} returned {e.response.status_code}: {e.response.text[:300]}") from e
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach {provider}: {e}") from e


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def chat_json(cfg: dict, system: str, user: str, timeout: float = 600) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    reply = _call(cfg, messages, timeout)
    try:
        return parse_json(reply)
    except ValueError:
        messages += [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "That was not valid JSON. Reply again with ONLY the JSON object."},
        ]
        try:
            return parse_json(_call(cfg, messages, timeout))
        except ValueError as e:
            raise LLMError(f"Model did not return valid JSON: {e}") from e
