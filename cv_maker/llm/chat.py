import json
import os
import re
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import httpx

_UNSET_MESSAGE = "No LLM configured: set provider in .env or on the Settings page"


class ProviderUnsetError(Exception):
    pass


class LLMError(RuntimeError):
    """A provider call failed; the message is meant to be shown to the user."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after  # seconds the provider asked us to wait, when it said


def _retry_after(headers) -> float | None:
    """Retry-After (seconds or an HTTP date) or X-RateLimit-Reset (epoch seconds or milliseconds)."""
    import time
    from email.utils import parsedate_to_datetime

    if not headers:
        return None
    value = headers.get("retry-after")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                return None
    reset = headers.get("x-ratelimit-reset")
    try:
        reset = float(reset)
    except (TypeError, ValueError):
        return None
    reset = reset / 1000 if reset > 1e11 else reset
    return max(0.0, reset - time.time()) if reset > 1e9 else reset


@runtime_checkable
class ChatModel(Protocol):
    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        ...


def _describe_failure(provider: str, status: int | None, detail: str) -> str:
    detail = (detail or "").strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    if status == 401:
        hint = f"{provider} rejected the API key (HTTP 401). Update it on the Settings page."
    elif status == 402:
        hint = f"{provider} says the account has no credit (HTTP 402)."
    elif status == 403:
        hint = f"{provider} refused the request (HTTP 403)."
    elif status == 404:
        hint = f"{provider} could not find that model (HTTP 404). Check the model name on the Settings page."
    elif status == 429:
        hint = f"{provider} rate-limited the request (HTTP 429). Wait a minute and retry, or pick another model."
    elif status and status >= 500:
        hint = f"{provider} had a server error (HTTP {status}). Retry shortly."
    elif status:
        hint = f"{provider} returned HTTP {status}."
    else:
        hint = f"{provider} request failed."
    return f"{hint} {detail}".strip() if detail else hint


def _http_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text[:300]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
        return str(body.get("message") or "")
    return ""


def _usage(tokens_in, tokens_out) -> dict | None:
    """Token counts as the provider reported them (shown in the nerdbar); None when it reported nothing."""
    if tokens_in is None and tokens_out is None:
        return None
    return {"in": int(tokens_in or 0), "out": int(tokens_out or 0)}


def _sdk_usage(response) -> dict | None:
    usage = getattr(response, "usage", None)
    return _usage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)) if usage else None


def _sdk_failure(provider: str, exc: Exception) -> LLMError:
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "message", None) or str(exc)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    return LLMError(_describe_failure(provider, status, detail), status=status, retry_after=_retry_after(headers))


def _post_json(provider: str, url: str, payload: dict, headers: dict | None, timeout: float) -> dict:
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise LLMError(f"{provider} did not answer within {int(timeout)}s. Retry, or pick a faster model.") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach {provider}: {exc}") from exc
    if response.status_code >= 400:
        raise LLMError(_describe_failure(provider, response.status_code, _http_error_detail(response)), status=response.status_code,
                       retry_after=_retry_after(response.headers))
    try:
        return response.json()
    except Exception as exc:
        raise LLMError(f"{provider} returned a non-JSON response.") from exc


class OpenAIAdapter:
    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        api_key = config.get("LLM_API_KEY")
        if not api_key:
            raise ProviderUnsetError(_UNSET_MESSAGE)
        try:
            import openai
        except ImportError as exc:
            raise ProviderUnsetError("openai SDK not installed: pip install cv-maker[openai]") from exc
        self._client = openai.OpenAI(api_key=api_key)
        self._model = config.get("LLM_MODEL") or "gpt-4o"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            self.last_usage = _sdk_usage(response)
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise _sdk_failure("OpenAI", exc) from exc


class AzureFoundryAdapter:
    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        api_key = config.get("LLM_API_KEY")
        endpoint = config.get("AZURE_OPENAI_ENDPOINT")
        if not api_key or not endpoint:
            raise ProviderUnsetError(_UNSET_MESSAGE)
        try:
            from openai import AzureOpenAI  # Azure OpenAI's client ships in the openai package
        except ImportError as exc:
            raise ProviderUnsetError("Azure needs the openai package: pip install cv-maker[azure]") from exc
        self._client = AzureOpenAI(api_key=api_key, api_version="2024-06-01", azure_endpoint=endpoint)
        self._model = config.get("LLM_MODEL") or "gpt-4o"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            self.last_usage = _sdk_usage(response)
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise _sdk_failure("Azure OpenAI", exc) from exc


class BedrockAdapter:
    """Anthropic models on Bedrock. Credentials come from the standard AWS chain, not LLM_API_KEY."""

    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        try:
            import boto3
        except ImportError as exc:
            raise ProviderUnsetError("bedrock SDK not installed: pip install cv-maker[bedrock]") from exc
        self._client = boto3.client("bedrock-runtime", region_name=config.get("AWS_DEFAULT_REGION") or "us-east-1")
        self._model = config.get("LLM_MODEL") or "anthropic.claude-3-sonnet-20240229-v1:0"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        body = {"messages": messages, "anthropic_version": "bedrock-2023-05-31", "max_tokens": 4096}
        try:
            response = self._client.invoke_model(
                modelId=self._model,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            payload = json.loads(response["body"].read().decode())
        except Exception as exc:
            raise _sdk_failure("Bedrock", exc) from exc
        usage = payload.get("usage") or {}
        self.last_usage = _usage(usage.get("input_tokens"), usage.get("output_tokens"))
        content = payload.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text") or content[0].get("content") or ""
            if text:
                return text
        if isinstance(payload.get("output"), dict):
            return payload["output"].get("message", {}).get("content", [{}])[0].get("text", "")
        return ""


class OllamaAdapter:
    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        self._model = config.get("LLM_MODEL") or "llama3"
        self._url = (config.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/") + "/api/chat"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        payload = {"model": self._model, "messages": messages, "stream": False}
        if json_mode:
            payload["format"] = "json"
        data = _post_json("Ollama", self._url, payload, None, timeout=300.0)
        self.last_usage = _usage(data.get("prompt_eval_count"), data.get("eval_count"))
        return data.get("message", {}).get("content") or ""


class GroqAdapter:
    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        api_key = config.get("LLM_API_KEY")
        if not api_key:
            raise ProviderUnsetError(_UNSET_MESSAGE)
        try:
            import groq
        except ImportError as exc:
            raise ProviderUnsetError("groq SDK not installed: pip install cv-maker[groq]") from exc
        self._client = groq.Groq(api_key=api_key)
        self._model = config.get("LLM_MODEL") or "llama-3.1-70b-versatile"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            self.last_usage = _sdk_usage(response)
            return response.choices[0].message.content or ""
        except Exception as exc:
            raise _sdk_failure("Groq", exc) from exc


class OpenRouterAdapter:
    last_usage: dict | None = None

    def __init__(self, config: Mapping[str, str]) -> None:
        api_key = config.get("LLM_API_KEY")
        if not api_key:
            raise ProviderUnsetError(_UNSET_MESSAGE)
        self._url = "https://openrouter.ai/api/v1/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "X-Title": "CV Tailor",
        }
        self._model = config.get("LLM_MODEL") or "google/gemini-2.0-flash"

    def complete(self, messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
        payload = {"model": self._model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = _post_json("OpenRouter", self._url, payload, self._headers, timeout=180.0)
        if data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            raise LLMError(_describe_failure("OpenRouter", code, message), status=code if isinstance(code, int) else None)
        usage = data.get("usage") or {}
        self.last_usage = _usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


_PROVIDERS = {
    "openai": OpenAIAdapter,
    "azure": AzureFoundryAdapter,
    "azure-foundry": AzureFoundryAdapter,
    "bedrock": BedrockAdapter,
    "ollama": OllamaAdapter,
    "groq": GroqAdapter,
    "openrouter": OpenRouterAdapter,
}

_KEYLESS_PROVIDERS = {"ollama", "bedrock"}


def get_chat_model(config: Mapping[str, str] | None = None):
    """Build the configured adapter. `config` defaults to the process environment."""
    config = os.environ if config is None else config
    provider = (config.get("LLM_PROVIDER") or "").strip().lower()
    if not provider:
        raise ProviderUnsetError(_UNSET_MESSAGE)
    factory = _PROVIDERS.get(provider)
    if factory is None:
        raise ProviderUnsetError(f"unknown provider: {provider}")
    if provider not in _KEYLESS_PROVIDERS and not config.get("LLM_API_KEY"):
        raise ProviderUnsetError(_UNSET_MESSAGE)
    return factory(config)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_json_reply(raw: str) -> dict:
    """Parse a model reply that should be a JSON object, tolerating code fences and reasoning tags."""
    text = _THINK.sub("", raw or "").strip()
    if not text:
        raise LLMError("The model returned an empty reply. Retry, or pick a different model on the Settings page.")
    candidates = [text]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    snippet = text[:120].replace("\n", " ")
    raise LLMError(f"The model's reply was not valid JSON (starts with: {snippet!r}). Retry, or pick a different model.")


REPAIR = "Your last reply was not valid JSON. Reply again with only the JSON object it asked for: no prose, no code fence."


def complete_json(model: ChatModel, prompt: str) -> dict:
    """Ask for a JSON object; falls back to plain mode if the provider rejects JSON mode. A reply that isn't valid
    JSON (models sometimes add prose or stop halfway) gets one more chance, then a clear error."""
    messages = [{"role": "user", "content": prompt}]
    json_mode = True
    try:
        raw = model.complete(messages, json_mode=True)
    except LLMError as exc:
        if exc.status in (400, 404, 422) and ("response_format" in str(exc) or "json" in str(exc).lower()):
            json_mode = False
            raw = model.complete(messages, json_mode=False)
        else:
            raise
    try:
        return parse_json_reply(raw)
    except LLMError:
        if not (raw or "").strip():
            raise  # an empty reply says nothing to repair
    retry = messages + [{"role": "assistant", "content": raw[:6000]}, {"role": "user", "content": REPAIR}]
    return parse_json_reply(model.complete(retry, json_mode=json_mode))


def check_connection(config: Mapping[str, str]) -> str:
    """Make one tiny call; returns a success message or raises ProviderUnsetError/LLMError."""
    model = get_chat_model(config)
    data = complete_json(model, 'Reply with exactly this JSON object and nothing else: {"ok": true}')
    if data.get("ok") is not True:
        raise LLMError(f"Connected, but the model did not follow the JSON instruction (replied {json.dumps(data)[:80]}).")
    return f"Connected: {config.get('LLM_PROVIDER')} / {config.get('LLM_MODEL')} answered correctly."
