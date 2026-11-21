"""Unit tests for the cross-slot multi-key Redis check (issue #183)."""
from __future__ import annotations

import pytest

from prxref import heuristics, redis_cluster
from prxref.triage import parse_unified_diff

CLUSTER_DOC = {"deploy/cache.yaml": "# managed Valkey, cluster mode enabled\n"}


def _diff(path: str, lines: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + body
    )


def _files(*diffs: str):
    return parse_unified_diff("".join(diffs))


def _java(*body: str) -> str:
    return _diff("src/Evictor.java", [
        "import org.springframework.data.redis.core.StringRedisTemplate;",
        "class Evictor {",
        *body,
        "}",
    ])


class _Repo:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.reads: list[str] = []

    def read(self, path: str):
        self.reads.append(path)
        return self.files.get(path)


def _run(files, repo: dict[str, str] | None = None):
    r = _Repo(repo if repo is not None else CLUSTER_DOC)
    return redis_cluster.crossslot_findings(files, read=r.read, listing=sorted(r.files)), r


SCAN_BATCH = (
    "  void evict() {",
    "    var batch = new ArrayList<String>();",
    "    cursor.forEachRemaining(batch::add);",
    "    redisTemplate.delete(batch);",
    "  }",
)


class TestCandidates:
    def test_the_issue_shape_is_flagged_on_the_delete_line(self):
        [f], _ = _run(_files(_java(*SCAN_BATCH)))
        assert (f.file, f.line, f.severity) == ("src/Evictor.java", 6, "warning")
        assert heuristics.is_deterministic(f)
        assert "deploy/cache.yaml:1" in f.body

    def test_a_single_key_delete_is_not_flagged(self):
        findings, repo = _run(_files(_java("  void evict(String key) { redisTemplate.delete(key); }")))
        assert findings == []
        assert repo.reads == []

    def test_a_collection_parameter_is_flagged(self):
        [_], _ = _run(_files(_java("  void evict(Collection<String> keys) { redisTemplate.unlink(keys); }")))

    def test_a_non_redis_file_is_not_flagged(self):
        files = _files(_diff("src/Store.java", [
            "class Store {",
            "  void drop() { var ids = new ArrayList<String>(); repository.delete(ids); }",
            "}",
        ]))
        assert _run(files)[0] == []

    def test_a_receiver_that_names_no_redis_client_is_not_flagged(self):
        assert _run(_files(_java("  void f() { var xs = new ArrayList<String>(); files.delete(xs); }")))[0] == []

    @pytest.mark.parametrize("line", [
        "    redisTemplate.opsForValue().multiGet(keys);",
        "    redisTemplate.delete(List.of(a, b));",
        "    jedis.del(k1, k2);",
    ])
    def test_other_multi_key_shapes_are_flagged(self, line):
        [_], _ = _run(_files(_java("  void f() {", line, "  }")))

    def test_a_python_spread_is_flagged(self):
        files = _files(_diff("app/cache.py", ["import redis", "r = redis.Redis()", "r.delete(*keys)"]))
        [_], _ = _run(files)

    def test_a_node_spread_is_flagged(self):
        files = _files(_diff("src/cache.ts", ["import Redis from 'ioredis';", "await redis.del(...keys);"]))
        [_], _ = _run(files)

    def test_a_shared_hash_tag_exempts_the_file(self):
        files = _files(_java('    String pattern = "{items}:*_item_7";', *SCAN_BATCH))
        assert _run(files)[0] == []

    @pytest.mark.parametrize("line", [
        '    log.info("deleting {} keys", batch.size());',
        '    String p = "${cache.prefix}_item";',
        "    String p = `${prefix}_item`;",
    ])
    def test_placeholders_are_not_hash_tags(self, line):
        [_], _ = _run(_files(_java(line, *SCAN_BATCH)))

    def test_a_python_f_string_is_not_a_hash_tag(self):
        lines = ["import redis", "r = redis.Redis()", 'p = f"{prefix}_item"', "r.delete(*keys)"]
        files = _files(_diff("app/cache.py", lines))
        [_], _ = _run(files)


class TestEvidence:
    @pytest.mark.parametrize(("path", "text"), [
        ("redis.conf", "cluster-enabled yes\n"),
        ("docker-compose.yml", "command: valkey-server --cluster-enabled yes\n"),
        ("infra/main.tf", 'resource "aws_elasticache_serverless_cache" "items" {}\n'),
        ("infra/stack.yaml", "Type: AWS::ElastiCache::ServerlessCache\n"),
        ("docs/ops.md", "The item cache is Amazon MemoryDB.\n"),
        ("docs/ops.md", "We run ElastiCache Serverless for the item cache.\n"),
        ("infra/redis.tf", "cluster_mode = true\n"),
        ("README.md", "Redis runs in cluster mode in every environment.\n"),
    ])
    def test_each_marker_is_evidence(self, path, text):
        [f], _ = _run(_files(_java(*SCAN_BATCH)), {path: text})
        assert f"{path}:1" in f.body

    @pytest.mark.parametrize(("path", "text"), [
        ("README.md", "The scheduler runs in cluster mode.\n"),
        ("src/main/resources/application.yml", "redis:\n  host: localhost\n"),
        ("src/Config.java", "new RedisClusterConfiguration(nodes);\n"),
        ("docs/ops.md", "Our API is serverless.\n"),
    ])
    def test_non_evidence_is_ignored(self, path, text):
        assert _run(_files(_java(*SCAN_BATCH)), {path: text})[0] == []

    def test_evidence_in_the_diff_needs_no_read(self):
        files = _files(_java(*SCAN_BATCH), _diff("redis.conf", ["cluster-enabled yes"]))
        [f], repo = _run(files, {})
        assert "redis.conf:1" in f.body
        assert repo.reads == []

    def test_no_reader_and_no_diff_evidence_means_no_finding(self):
        assert redis_cluster.crossslot_findings(_files(_java(*SCAN_BATCH))) == []

    def test_reads_are_bounded(self):
        repo = {f"conf/c{i:03}.yml": "x: 1\n" for i in range(100)}
        findings, r = _run(_files(_java(*SCAN_BATCH)), repo)
        assert findings == []
        assert len(r.reads) == redis_cluster.MAX_EVIDENCE_FILES

    def test_cache_named_paths_are_read_first(self):
        repo = {f"conf/c{i:03}.yml": "x: 1\n" for i in range(100)}
        repo["deep/nested/redis/settings.txt"] = "cluster-enabled yes\n"
        [_], r = _run(_files(_java(*SCAN_BATCH)), repo)
        assert r.reads[0] == "deep/nested/redis/settings.txt"

    def test_no_listing_tries_the_literal_paths(self):
        r = _Repo({"README.md": "Valkey, cluster mode.\n"})
        [_] = redis_cluster.crossslot_findings(_files(_java(*SCAN_BATCH)), read=r.read, listing=None)

    def test_a_read_that_raises_is_skipped(self):
        def read(path):
            raise OSError("boom")
        assert redis_cluster.crossslot_findings(_files(_java(*SCAN_BATCH)), read=read, listing=["a.yml"]) == []
