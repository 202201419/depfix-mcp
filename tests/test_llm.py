import json
import urllib.error

import pytest

from depfix.embed import normalize
from depfix.llm import LLMRequest, PolicyLLM, RecordingLLM, ReplayLLM, ReplayMiss
from depfix.ollama import (
    OllamaClient,
    OllamaEmbedder,
    OllamaError,
    OllamaLLM,
    check_ollama,
)
from tests.helpers import FakeOllama

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def req(text: str = "hi", **over) -> LLMRequest:
    base = {
        "model": "m",
        "messages": [{"role": "user", "content": text}],
        "schema": SCHEMA,
        "options": {"seed": 0},
    }
    base.update(over)
    return LLMRequest(**base)


# ---- request keys, record and replay --------------------------------------------------------------


def test_identical_requests_have_identical_keys_and_any_change_alters_it():
    assert req().key() == req().key()
    assert req().key() != req("other").key()
    assert req().key() != req(model="m2").key()
    assert req().key() != req(options={"seed": 1}).key()


def test_record_then_replay_round_trip_and_stores_no_prompt_text(tmp_path):
    path = tmp_path / "rec.jsonl"
    rec = RecordingLLM(PolicyLLM(lambda r: '{"ok": true}'), path)
    assert rec.complete(req("SECRET DOC TEXT")) == '{"ok": true}'
    rec.complete(req("SECRET DOC TEXT"))  # the same request is not recorded twice
    assert len(path.read_text().splitlines()) == 1
    assert "SECRET DOC TEXT" not in path.read_text()  # only a hash and the answer are stored
    assert ReplayLLM(path).complete(req("SECRET DOC TEXT")) == '{"ok": true}'


def test_replay_miss_explains_how_to_fix_it(tmp_path):
    with pytest.raises(ReplayMiss, match="make record"):
        ReplayLLM(tmp_path / "none.jsonl").complete(req())


# ---- the Ollama client against a real local server --------------------------------------------------


def chat_ok(_m, path, body):
    if path == "/api/chat":
        return 200, {
            "message": {"content": '{"ok": true}'},
            "eval_count": 20,
            "eval_duration": 1_000_000_000,
        }
    return 404, {"error": "nope"}


def test_chat_request_asks_for_structured_json_without_streaming():
    srv = FakeOllama(chat_ok)
    try:
        llm = OllamaLLM(OllamaClient(srv.url))
        assert llm.complete(req()) == '{"ok": true}'
        _, path, body = srv.requests[0]
        assert path == "/api/chat" and body["stream"] is False
        assert body["format"] == SCHEMA and body["options"] == {"seed": 0}
        assert body["model"] == "m" and body["messages"][0]["content"] == "hi"
        assert llm.last_stats["eval_count"] == 20
    finally:
        srv.stop()


def test_unknown_model_error_says_how_to_pull_it():
    srv = FakeOllama(lambda *a: (404, {"error": "model 'zzz' not found"}))
    try:
        with pytest.raises(OllamaError, match="ollama pull"):
            OllamaLLM(OllamaClient(srv.url)).complete(req(model="zzz"))
    finally:
        srv.stop()


def test_server_errors_and_bad_replies_are_clean_errors():
    srv = FakeOllama(lambda *a: (500, {"error": "boom"}))
    try:
        with pytest.raises(OllamaError, match="HTTP 500"):
            OllamaLLM(OllamaClient(srv.url)).complete(req())
    finally:
        srv.stop()
    srv = FakeOllama(lambda *a: (200, b"not json"))
    try:
        with pytest.raises(OllamaError, match="not JSON"):
            OllamaLLM(OllamaClient(srv.url)).complete(req())
    finally:
        srv.stop()
    srv = FakeOllama(lambda *a: (200, {"unexpected": 1}))
    try:
        with pytest.raises(OllamaError, match="shape"):
            OllamaLLM(OllamaClient(srv.url)).complete(req())
    finally:
        srv.stop()


def test_a_server_that_is_not_running_gives_an_actionable_message():
    srv = FakeOllama(chat_ok)
    url = srv.url
    srv.stop()
    with pytest.raises(OllamaError, match="Is it running"):
        OllamaLLM(OllamaClient(url, timeout=3)).complete(req())


def test_remote_servers_are_refused_by_default_because_prompts_contain_your_code(monkeypatch):
    with pytest.raises(OllamaError, match="only a local Ollama"):
        OllamaClient("http://203.0.113.5:11434")
    with pytest.raises(OllamaError, match="only a local Ollama"):
        OllamaClient("http://example.com:11434")
    OllamaClient("http://localhost:11434")
    OllamaClient("http://127.0.0.1:11434")
    monkeypatch.setenv("DEPFIX_ALLOW_REMOTE_OLLAMA", "1")
    OllamaClient("http://203.0.113.5:11434")


def test_embedder_applies_task_prefixes_batches_and_normalises():
    def handler(_m, path, body):
        return 200, {"embeddings": [[3.0, 4.0] for _ in body["input"]]}

    srv = FakeOllama(handler)
    try:
        emb = OllamaEmbedder(OllamaClient(srv.url), "nomic-embed-text", batch=2)
        out = emb.embed(["a", "b", "c", "d", "e"], "document")
        assert len(out) == 5 and out[0] == pytest.approx([0.6, 0.8])
        assert len(srv.requests) == 3  # 5 texts in batches of 2
        assert srv.requests[0][2]["input"] == ["search_document: a", "search_document: b"]
        emb.embed(["q"], "query")
        assert srv.requests[-1][2]["input"] == ["search_query: q"]
        assert emb.name == "ollama:nomic-embed-text"
    finally:
        srv.stop()


def test_embedder_rejects_a_wrong_number_of_vectors():
    srv = FakeOllama(lambda *a: (200, {"embeddings": [[1.0]]}))
    try:
        with pytest.raises(OllamaError, match="wrong number"):
            OllamaEmbedder(OllamaClient(srv.url)).embed(["a", "b"], "query")
    finally:
        srv.stop()


def test_normalize_handles_zero_vectors():
    assert normalize([0.0, 0.0]) == [0.0, 0.0]


def full_server(models):
    def handler(method, path, body):
        if path == "/api/tags":
            return 200, {"models": [{"name": n} for n in models]}
        if path == "/api/embed":
            return 200, {"embeddings": [[0.1, 0.2, 0.3]]}
        if path == "/api/chat":
            return 200, {
                "message": {"content": '{"ok": true}'},
                "eval_count": 50,
                "eval_duration": 2_000_000_000,
            }
        return 404, {}

    return FakeOllama(handler)


def test_llm_check_passes_when_everything_works_and_reports_speed():
    srv = full_server(["qwen2.5-coder:7b", "nomic-embed-text:latest"])
    try:
        rows = check_ollama(OllamaClient(srv.url), "qwen2.5-coder:7b", "nomic-embed-text")
        assert all(ok for _, ok, _ in rows), rows
        assert "25.0 tokens/s" in rows[-1][2]
    finally:
        srv.stop()


def test_llm_check_names_the_missing_model_and_the_command_to_fix_it():
    srv = full_server(["nomic-embed-text"])
    try:
        rows = check_ollama(OllamaClient(srv.url), "qwen2.5-coder:7b", "nomic-embed-text")
        bad = [r for r in rows if not r[1]]
        assert len(bad) == 1 and "ollama pull qwen2.5-coder:7b" in bad[0][2]
    finally:
        srv.stop()


def test_llm_check_when_ollama_is_down():
    srv = full_server([])
    url = srv.url
    srv.stop()
    rows = check_ollama(OllamaClient(url, timeout=3), "m", "e")
    assert rows[0][0] == "Ollama is reachable" and rows[0][1] is False


def test_the_policy_double_records_its_calls():
    p = PolicyLLM(lambda r: "x")
    p.complete(req())
    assert len(p.calls) == 1 and json.loads('{"a": 1}')["a"] == 1
    assert isinstance(urllib.error.URLError("x"), Exception)
