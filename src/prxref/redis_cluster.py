"""Deterministic cross-slot check for multi-key Redis commands (issue #183).

A multi-key Redis command (``DEL``, ``UNLINK``, ``EXISTS``, ``MGET``,
``MSET``) fails on a cluster-mode server with ``CROSSSLOT Keys in request
don't hash to the same slot`` unless every key shares one hash slot. Keys
gathered by a ``SCAN`` (or built from a list) almost never do, and a unit
suite running against a standalone Redis passes anyway. This module flags an
ADDED line that hands a Redis client such a command over a collection of
keys when the repository says its cache runs in cluster mode.

Like :mod:`prxref.ci_wiring` it is a companion to :mod:`prxref.heuristics`
under the same doctrine: no model in the loop, every finding ends its body
with " (deterministic check, no model)" (the suffix is imported from
heuristics), so :func:`prxref.heuristics.is_deterministic` exempts it from
severity consistency. Every finding is a ``warning``; the check never
changes the verdict by itself.

A call is a candidate when all of these hold on an added line of a file whose
diff lines mention Redis (``redis``, ``valkey``, ``jedis`` or ``lettuce``,
any case):

- a method ``delete``, ``del``, ``unlink``, ``exists``, ``mget``, ``mset``,
  ``multiGet`` or ``multiSet`` is called on a receiver naming Redis, a
  template, a client, a connection, a cache or an ``opsFor...()`` view, or
  on a name the file assigns from a Redis constructor (``r = redis.Redis()``);
- its argument is several keys: a spread (``*keys``, ``...keys``), two or
  more arguments, an inline collection (``[``, ``List.of``,
  ``Arrays.asList``, ``Set.of``), any argument to ``mget``/``mset``/
  ``multiGet``/``multiSet``, or a name the file's diff lines declare as a
  collection (``var batch = new ArrayList<>()``, ``Collection<String> keys``,
  ``keys = []``, ``keys = list(...)``);
- no string literal on the file's added lines carries a hash tag
  (``"{user}:*"``), the one way to pin keys to one slot. ``{}`` log
  placeholders, ``${...}`` and f-strings or template literals do not count.

Cluster evidence is any line, in the diff (added or context) or in a head
file of the repository, that says the cache deploys in cluster mode:

- ``cluster-enabled yes`` (a ``redis.conf`` or a server command line);
- "cluster mode" / ``cluster_mode`` / ``cluster-mode`` / ``clusterMode`` on
  a line of a file that mentions Redis, Valkey, ElastiCache or MemoryDB;
- a serverless ElastiCache or Valkey/Redis cache (``serverless`` on a line
  naming one of them, ``aws_elasticache_serverless_cache``,
  ``AWS::ElastiCache::ServerlessCache``) or Amazon MemoryDB, both of which
  always run in cluster mode.

Cluster-aware client classes (``RedisClusterConfiguration``,
``RedisCluster``) are deliberately NOT evidence: Spring Data Redis's and
redis-py's cluster clients split a cross-slot ``DEL`` by slot themselves.

Repository reads go through a ``read`` callable the orchestrator passes in
(the forge's head-sha reads or ``--repo-dir``), and happen only when the
diff holds a candidate call and the diff itself holds no evidence. At most
:data:`MAX_EVIDENCE_FILES` files are read, chosen from the listing: paths
naming a cache first (``redis``, ``valkey``, ``cache``, ``elasticache``,
``memorydb``), then configuration, infrastructure and docs files, shallow
paths before deep ones. With no listing, :data:`_LITERAL_PATHS` are tried.

Known false negatives: a collection built in another file, a hash tag built
at run time, evidence in a file the selection skips. Known false positives:
a cluster-aware client that splits multi-key commands (Spring Data's
cluster connection, redis-py's ``RedisCluster``) configured against the same
cluster the evidence names.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

from .heuristics import _BODY_SUFFIX
from .triage import FileDiff, Finding

MAX_EVIDENCE_FILES = 40

_REDIS_FILE_RE = re.compile(r"(?i:redis|valkey|jedis|lettuce)")
_CACHE_WORD_RE = re.compile(r"(?i)redis|valkey|elasticache|memorydb")

_CALL_RE = re.compile(
    r"(?<![\w$])(?P<recv>[A-Za-z_$][\w$]*(?:\(\))?(?:\.[A-Za-z_$][\w$]*(?:\(\))?)*)"
    r"\.(?P<method>delete|del|unlink|exists|mget|mset|multiGet|multiSet)\(\s*(?P<args>[^()]*(?:\([^()]*\)[^()]*)*)\)"
)
_RECEIVER_RE = re.compile(r"(?i)redis|valkey|jedis|lettuce|template|client|conn|cache|commands|opsfor")
_ALWAYS_MULTI = frozenset({"mget", "mset", "multiGet", "multiSet"})
_SPREAD_RE = re.compile(r"^(?:\*(?!\*)|\.\.\.)\s*\w")
_INLINE_COLLECTION_RE = re.compile(r"^(?:\[|(?:List|Set|Arrays)\.(?:of|asList)\(|new\s+\w*(?:List|Set)\b)")
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_COLLECTION_TYPES = r"(?:List|Set|Collection|Iterable|ArrayList|HashSet|LinkedList|LinkedHashSet|TreeSet)"
_COLLECTION_VALUE = (
    r"(?:new\s+" + _COLLECTION_TYPES + r"\b|\[|list\(|set\(|(?:List|Set)\.of\(|Arrays\.asList\(|"
    r"\w+\.stream\(\)|\w+\.collect\()"
)

_STRING_RE = re.compile(r"(?P<prefix>[A-Za-z]*)(?P<q>[\"'`])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)")
_HASH_TAG_RE = re.compile(r"(?<![$#%])\{[A-Za-z0-9_:.\-]+\}")

_CLUSTER_ENABLED_RE = re.compile(r"(?i)cluster-enabled\s+yes")
_CLUSTER_MODE_RE = re.compile(r"(?i)cluster[\s_-]?mode")
_SERVERLESS_RE = re.compile(r"(?i)serverless")
_ALWAYS_CLUSTER_RE = re.compile(
    r"(?i)aws_elasticache_serverless_cache|AWS::ElastiCache::ServerlessCache|memorydb"
)

_PRIORITY_PATH_RE = re.compile(r"(?i)redis|valkey|cache|elasticache|memorydb")
_CONFIG_SUFFIXES = frozenset({
    ".yml", ".yaml", ".properties", ".conf", ".toml", ".tf", ".json", ".env", ".ini", ".md", ".txt",
})
_LITERAL_PATHS = (
    "README.md",
    "redis.conf",
    "docker-compose.yml",
    "src/main/resources/application.yml",
    "src/main/resources/application.yaml",
    "src/main/resources/application.properties",
)


@dataclass(frozen=True)
class MultiKeyCall:
    """One added line handing a Redis client a multi-key command."""

    path: str
    line: int
    method: str
    call: str


def _added_lines(file: FileDiff) -> Iterator[tuple[int, str]]:
    for hunk in file.hunks:
        for ln in hunk.lines:
            if ln.kind == "+" and ln.new_line is not None:
                yield ln.new_line, ln.text


def _head_lines(file: FileDiff) -> Iterator[tuple[int, str]]:
    """Added and context lines of ``file``: the head side of its diff."""
    for hunk in file.hunks:
        for ln in hunk.lines:
            if ln.kind in ("+", " ") and ln.new_line is not None:
                yield ln.new_line, ln.text


def _split_args(args: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in args:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _declares_collection(name: str, text: str) -> bool:
    n = re.escape(name)
    typed = re.compile(_COLLECTION_TYPES + r"(?:<[^;=()]*>)?\s+" + n + r"\b")
    assigned = re.compile(r"(?<![\w$.])" + n + r"\s*(?::[^=]+)?=\s*" + _COLLECTION_VALUE)
    return bool(typed.search(text) or assigned.search(text))


def _names_client(receiver: str, file_text: str) -> bool:
    """True when ``receiver`` names a Redis client, or the file assigns it from one (``r = redis.Redis()``)."""
    if _RECEIVER_RE.search(receiver):
        return True
    name = re.escape(receiver.rsplit(".", 1)[-1])
    return bool(re.search(r"(?<![\w$.])" + name + r"\s*=[^=\n]*" + _REDIS_FILE_RE.pattern, file_text))


def _is_multi(method: str, args: str, file_text: str) -> bool:
    parts = _split_args(args)
    if not parts:
        return False
    if method in _ALWAYS_MULTI:
        return True
    if len(parts) >= 2:
        return True
    arg = parts[0]
    if _SPREAD_RE.match(arg) or _INLINE_COLLECTION_RE.match(arg):
        return True
    return bool(_IDENT_RE.match(arg)) and _declares_collection(arg, file_text)


def _has_hash_tag(lines: list[str]) -> bool:
    for text in lines:
        for m in _STRING_RE.finditer(text):
            if m.group("q") == "`" or "f" in m.group("prefix").lower():
                continue
            if _HASH_TAG_RE.search(m.group("s")):
                return True
    return False


def multi_key_calls(files: list[FileDiff]) -> list[MultiKeyCall]:
    """Every candidate multi-key call on an added line, sorted by path and line."""
    calls: list[MultiKeyCall] = []
    for file in files:
        if file.status == "removed" or not file.hunks:
            continue
        head = [text for _, text in _head_lines(file)]
        file_text = "\n".join(head)
        if not _REDIS_FILE_RE.search(file_text):
            continue
        added = list(_added_lines(file))
        if _has_hash_tag([text for _, text in added]):
            continue
        for line, text in added:
            for m in _CALL_RE.finditer(text):
                if not _names_client(m.group("recv"), file_text):
                    continue
                if _is_multi(m.group("method"), m.group("args"), file_text):
                    calls.append(MultiKeyCall(file.path, line, m.group("method"), m.group(0)))
                    break
    return sorted(calls, key=lambda c: (c.path, c.line))


def _evidence_in(path: str, lines: list[tuple[int, str]]) -> tuple[str, int, str] | None:
    """The first line of ``lines`` saying the cache runs in cluster mode, as ``(path, line, text)``."""
    names_cache = any(_CACHE_WORD_RE.search(text) for _, text in lines) or bool(_CACHE_WORD_RE.search(path))
    for number, text in lines:
        if (
            _CLUSTER_ENABLED_RE.search(text)
            or _ALWAYS_CLUSTER_RE.search(text)
            or (_SERVERLESS_RE.search(text) and _CACHE_WORD_RE.search(text))
            or (_CLUSTER_MODE_RE.search(text) and names_cache)
        ):
            return path, number, text.strip()
    return None


def _selected_paths(listing: Collection[str] | None) -> list[str]:
    if listing is None:
        return list(_LITERAL_PATHS)
    chosen = []
    for path in listing:
        p = PurePosixPath(path)
        name = p.name.lower()
        priority = bool(_PRIORITY_PATH_RE.search(path))
        config = p.suffix.lower() in _CONFIG_SUFFIXES or name.startswith(("readme", "dockerfile", "docker-compose"))
        if priority or config:
            chosen.append((0 if priority else 1, len(p.parts), path))
    return [path for *_, path in sorted(chosen)[:MAX_EVIDENCE_FILES]]


def cluster_evidence(
    files: list[FileDiff],
    *,
    read: Callable[[str], str | None] | None = None,
    listing: Collection[str] | None = None,
) -> tuple[str, int, str] | None:
    """The first cluster-mode evidence line, in the diff first, then in the repository; None when absent.

    Never raises on a failed read: a read that returns None or raises is skipped.
    """
    for file in sorted(files, key=lambda f: f.path):
        found = _evidence_in(file.path, list(_head_lines(file)))
        if found:
            return found
    if read is None:
        return None
    diff_paths = {f.path for f in files}
    for path in _selected_paths(listing):
        if path in diff_paths:
            continue
        try:
            text = read(path)
        except Exception:  # noqa: BLE001
            text = None
        if not text:
            continue
        found = _evidence_in(path, list(enumerate(text.splitlines(), start=1)))
        if found:
            return found
    return None


def crossslot_findings(
    files: list[FileDiff],
    *,
    read: Callable[[str], str | None] | None = None,
    listing: Collection[str] | None = None,
) -> list[Finding]:
    """Flag a multi-key Redis command over many keys when the repo deploys a cluster-mode cache.

    Returns one ``warning`` (confidence 0.9) per candidate call from
    :func:`multi_key_calls`, anchored on the call's own line, when
    :func:`cluster_evidence` finds evidence; ``[]`` otherwise, and ``[]``
    with no read at all when the diff holds no candidate call.
    """
    calls = multi_key_calls(files)
    if not calls:
        return []
    evidence = cluster_evidence(files, read=read, listing=listing)
    if evidence is None:
        return []
    ev_path, ev_line, ev_text = evidence
    findings = []
    for call in calls:
        body = (
            f"`{call.call}` sends one multi-key `{call.method}` over a collection of keys, and "
            f"{ev_path}:{ev_line} says the cache runs in cluster mode (`{ev_text[:160]}`). On a "
            "cluster-mode Redis or Valkey a multi-key command fails with `CROSSSLOT Keys in "
            "request don't hash to the same slot` unless every key shares one hash slot, and keys "
            "from a SCAN or a list almost never do; a standalone test server accepts it, so tests "
            "pass. Send one single-key DEL/UNLINK (or GET/SET) per key, pipelined, group the keys "
            "by slot, or give them a shared `{hash tag}`."
            + _BODY_SUFFIX
        )
        findings.append(Finding(
            file=call.path,
            line=call.line,
            severity="warning",
            confidence=0.9,
            title=f"Multi-key `{call.method}` on a cluster-mode cache can fail with CROSSSLOT",
            body=body,
        ))
    return findings
