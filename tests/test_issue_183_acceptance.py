"""Issue #183 acceptance: a multi-key DEL of SCAN results on a cluster-mode cache, end to end.

Every review here runs the production local path, ``cli._run_review`` with
``--diff-file`` and ``--repo-dir``, over a synthetic Java project written into
``tmp_path``: the issue's own repro, an eviction that SCANs for ``*_item_7``
and deletes the batch with one ``redisTemplate.delete(batch)``. The LLM is the
real openai-compat HTTP client talking to a local scripted server that answers
every prompt with ``{"findings": []}``, so any finding in a result is a
deterministic one. The cluster-mode fact lives OUTSIDE the diff, in a repo
file the PR does not touch, as it did in the issue.
"""
from __future__ import annotations

import json

import pytest

from prxref import cli, heuristics
from tests.test_integration import MockOpenAIServer, _completion

EVICTOR = "src/main/java/com/example/cache/ItemCacheEvictor.java"
NO_FINDINGS = json.dumps({"findings": []})

EVICTOR_LINES = [
    "package com.example.cache;",
    "",
    "import java.util.ArrayList;",
    "import org.springframework.data.redis.core.Cursor;",
    "import org.springframework.data.redis.core.ScanOptions;",
    "import org.springframework.data.redis.core.StringRedisTemplate;",
    "",
    "public class ItemCacheEvictor {",
    "    private final StringRedisTemplate redisTemplate;",
    "",
    "    public ItemCacheEvictor(StringRedisTemplate redisTemplate) {",
    "        this.redisTemplate = redisTemplate;",
    "    }",
    "",
    "    void deleteMatching(String pattern) {",
    "        var options = ScanOptions.scanOptions().match(pattern).count(1000).build();",
    "        try (Cursor<String> cursor = redisTemplate.scan(options)) {",
    "            var batch = new ArrayList<String>();",
    "            cursor.forEachRemaining(batch::add);",
    "            if (!batch.isEmpty()) redisTemplate.delete(batch);",
    "        }",
    "    }",
    "}",
]
DELETE_LINE = 20


def _patch() -> str:
    body = "".join(f"+{line}\n" for line in EVICTOR_LINES)
    return (
        f"diff --git a/{EVICTOR} b/{EVICTOR}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{EVICTOR}\n"
        f"@@ -0,0 +1,{len(EVICTOR_LINES)} @@\n"
        + body
    )


@pytest.fixture(scope="module")
def llm_server():
    server = MockOpenAIServer(routes={"fast": _completion(NO_FINDINGS, "stop")})
    base_url = server.start()
    yield base_url
    server.stop()


@pytest.fixture
def review(monkeypatch, llm_server, tmp_path):
    """Write the repo (the evictor plus ``extra`` files) and the patch, run one review, return its JSON payload."""

    def _review(extra: dict[str, str]) -> dict:
        repo = tmp_path / "repo"
        files = {EVICTOR: "\n".join(EVICTOR_LINES) + "\n", "README.md": "# Items service\n", **extra}
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        diff = tmp_path / "pr.patch"
        diff.write_text(_patch())
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", llm_server)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        result = cli._run_review(None, diff_file=str(diff), repo_dir=str(repo))
        return json.loads(json.dumps(cli._build_json_result(result)))

    return _review


def _crossslot(payload: dict) -> list[dict]:
    return [
        f for f in payload["findings"]
        if f["body"].endswith(heuristics._BODY_SUFFIX) and "CROSSSLOT" in f["title"]
    ]


def test_the_issue_case_is_flagged_when_a_repo_doc_names_cluster_mode(review):
    payload = review({
        "deploy/cache.yaml": "# Item cache: managed Valkey, always runs in cluster mode\nengine: valkey\n",
    })
    [finding] = _crossslot(payload)
    assert finding["file"] == EVICTOR
    assert finding["line"] == DELETE_LINE
    assert finding["severity"] == "warning"
    assert "deploy/cache.yaml:1" in finding["body"]
    assert "UNLINK" in finding["body"]


def test_a_redis_conf_with_cluster_enabled_is_evidence(review):
    payload = review({"docker/redis.conf": "port 6379\ncluster-enabled yes\n"})
    [finding] = _crossslot(payload)
    assert "docker/redis.conf:2" in finding["body"]


def test_no_cluster_evidence_means_no_finding(review):
    payload = review({"deploy/cache.yaml": "engine: valkey\nport: 6379\n"})
    assert _crossslot(payload) == []
