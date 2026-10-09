import os
import sys
import types

import httpx
import pytest

from cv_maker.llm.chat import LLMError, ProviderUnsetError, complete_json, get_chat_model, parse_json_reply
from cv_maker.jobs.match import match_profile
from cv_maker.jobs.requirements import JobRequirements, MustHave
from cv_maker.models import Profile, Skill


def test_factory_unset(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(ProviderUnsetError, match="set provider in .env"):
        get_chat_model()


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    with pytest.raises(ProviderUnsetError, match="unknown provider: unknown"):
        get_chat_model()


@pytest.mark.parametrize("provider", ["openai", "azure", "bedrock", "groq", "openrouter"])
def test_each_provider_raises_when_unset(monkeypatch, provider):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    if provider == "azure":
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
        monkeypatch.setitem(sys.modules, "openai", None)
    elif provider == "openai":
        monkeypatch.setitem(sys.modules, "openai", None)
    elif provider == "bedrock":
        monkeypatch.setitem(sys.modules, "boto3", None)
    elif provider == "groq":
        monkeypatch.setitem(sys.modules, "groq", None)
    elif provider == "openrouter":
        monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ProviderUnsetError):
        get_chat_model()


def test_ollama_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert get_chat_model() is not None


def test_config_mapping_overrides_environment(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    model = get_chat_model({"LLM_PROVIDER": "openrouter", "LLM_API_KEY": "k", "LLM_MODEL": "some/model"})
    assert type(model).__name__ == "OpenRouterAdapter"


@pytest.mark.parametrize("raw", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    "Sure! Here it is:\n```\n{\"a\": 1}\n```\nAnything else?",
    '<think>user wants JSON {not this}</think>\n{"a": 1}',
    'Result: {"a": 1} -- done',
])
def test_parse_json_reply_tolerates_wrappers(raw):
    assert parse_json_reply(raw) == {"a": 1}


def test_parse_json_reply_explains_failure():
    with pytest.raises(LLMError, match="not valid JSON"):
        parse_json_reply("I cannot help with that")
    with pytest.raises(LLMError, match="empty reply"):
        parse_json_reply("<think>hmm</think>")


def _openrouter(monkeypatch, status, body):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("httpx.post", fake_post)
    return get_chat_model({"LLM_PROVIDER": "openrouter", "LLM_API_KEY": "k", "LLM_MODEL": "m"})


def test_rejected_key_message_points_to_settings(monkeypatch):
    model = _openrouter(monkeypatch, 401, {"error": {"message": "User not found.", "code": 401}})
    with pytest.raises(LLMError) as info:
        model.complete([{"role": "user", "content": "hi"}])
    assert info.value.status == 401
    assert "rejected the API key" in str(info.value) and "Settings" in str(info.value)
    assert "User not found." in str(info.value)


def test_unknown_model_message_names_the_model_problem(monkeypatch):
    model = _openrouter(monkeypatch, 400, {"error": {"message": "deepseek/deepseek-r1:free is not a valid model ID"}})
    with pytest.raises(LLMError, match="not a valid model ID"):
        model.complete([{"role": "user", "content": "hi"}])


def test_complete_json_retries_without_json_mode_when_unsupported():
    calls = []

    class Picky:
        def complete(self, messages, *, json_mode=False):
            calls.append(json_mode)
            if json_mode:
                raise LLMError("response_format is not supported by this model", status=400)
            return '```json\n{"ok": true}\n```'

    assert complete_json(Picky(), "x") == {"ok": True}
    assert calls == [True, False]


def test_openai_adapter_json_mode(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    captured = {}

    class FakeMessage:
        content = "ok"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(model=None, messages=None, **kwargs):
            captured["model"] = model
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, *args, **kwargs):
            pass

        chat = FakeChat()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    model = get_chat_model()
    assert model.complete([{"role": "user", "content": "hi"}], json_mode=True) == "ok"
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}


def test_groq_adapter_json_mode(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_MODEL", "groq-test")
    captured = {}

    class FakeMessage:
        content = "ok"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(model=None, messages=None, **kwargs):
            captured["model"] = model
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroq:
        def __init__(self, *args, **kwargs):
            pass

        chat = FakeChat()

    monkeypatch.setitem(sys.modules, "groq", types.SimpleNamespace(Groq=FakeGroq))
    model = get_chat_model()
    assert model.complete([{"role": "user", "content": "hi"}], json_mode=True) == "ok"
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}


def test_ollama_adapter_json_mode(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama-test")
    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"message": {"content": "ok"}}, request=request)

    monkeypatch.setattr("httpx.post", fake_post, raising=False)
    model = get_chat_model()
    assert model.complete([{"role": "user", "content": "hi"}], json_mode=True) == "ok"
    assert captured["json"]["format"] == "json"


def test_azure_adapter_uses_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_PROVIDER", "azure")
    monkeypatch.setenv("LLM_MODEL", "azure-test")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    captured = {}

    class FakeMessage:
        content = "ok"

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(model=None, messages=None, **kwargs):
            captured["model"] = model
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeAzureOpenAI:
        def __init__(self, *args, **kwargs):
            captured["init"] = kwargs

        chat = FakeChat()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AzureOpenAI=FakeAzureOpenAI))
    model = get_chat_model()
    assert model.complete([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["init"]["azure_endpoint"] == "https://example.openai.azure.com"


def test_python_covered_kubernetes_missing():
    profile = Profile(
        name="A",
        email="a@x.com",
        phone="",
        location="",
        links={},
        target_role=None,
        experiences=[],
        education=[],
        skills=[Skill(name="Python", source="cv")],
        extras={},
    )
    reqs = JobRequirements(
        must_have=[
            MustHave(term="Python", original="Python"),
            MustHave(term="Kubernetes", original="Kubernetes"),
        ],
        nice_to_have=[],
        tools=[],
        seniority="",
        domain="",
        education="",
        language="",
        work_auth="",
    )
    result = match_profile(profile, reqs)
    by_term = {g.term: g for g in result.gaps}
    assert by_term["Python"].same and by_term["Python"].status == "partial"  # listed, not yet shown in a role
    assert by_term["Kubernetes"].status == "missing"
    assert [q.term for q in result.questions if not q.optional] == ["Kubernetes"]
    assert [q.term for q in result.questions if q.optional] == ["Python"]
