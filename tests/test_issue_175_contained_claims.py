"""Issue #175: a failure claim the PR's own guard rules out is lowered, not posted as a WARNING.

Both of the issue's synthetic repros, as written there:

- case one: ``ConfigStore.read`` can return null, the WARNING says a caller
  then fails on it, and the only caller the same PR adds (``ConfigParser``,
  another file, so another chunk) rejects null or blank with a typed
  ``InvalidConfigException``;
- case two: a WARNING that a compensating delete-by-name can remove a
  pre-existing account, while an absence check a few lines above in the same
  method throws first.

``contained_claims.apply_contained_claim_check`` lowers each to
``outofscope`` (minor) with the guard named, through ``orchestrate_review``
too, and leaves the unguarded variants of both alone.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator
from prxref.contained_claims import CONTAINED_REASONS, apply_contained_claim_check
from prxref.quality import CONTAINMENT_NOTE_SUFFIX, apply_containment_note
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM


def _new_file(path: str, body: str) -> str:
    lines = body.splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


STORE = """package config.store;

public class ConfigStore {
    /** Returns the current config text. */
    public String read(String id) {
        return client.getValue(id).text();   // provider returns null when the entry is binary
    }
}"""
PARSER_GUARDED = """package config.parser;

public class ConfigParser {
    public Config parse(String json) {
        if (json == null || json.isBlank()) {
            throw new InvalidConfigException("config is empty");
        }
        return Config.from(json);
    }
}"""
PARSER_UNGUARDED = """package config.parser;

public class ConfigParser {
    public Config parse(String json) {
        return Config.from(json.trim());
    }
}"""
WRITER = """def publish(client, config_id, text):
    client.put_value(Id=config_id, Text=text)   # always text, never binary
"""

CASE_ONE_DIFF = (
    _new_file("store/src/main/java/config/store/ConfigStore.java", STORE)
    + _new_file("parser/src/main/java/config/parser/ConfigParser.java", PARSER_GUARDED)
    + _new_file("writer/store.py", WRITER)
)
CASE_ONE_UNGUARDED_DIFF = (
    _new_file("store/src/main/java/config/store/ConfigStore.java", STORE)
    + _new_file("parser/src/main/java/config/parser/ConfigParser.java", PARSER_UNGUARDED)
    + _new_file("writer/store.py", WRITER)
)
CASE_ONE_NO_GUARD_NO_WRITER_DIFF = (
    _new_file("store/src/main/java/config/store/ConfigStore.java", STORE)
    + _new_file("parser/src/main/java/config/parser/ConfigParser.java", PARSER_UNGUARDED)
)
CASE_ONE = {
    "file": "store/src/main/java/config/store/ConfigStore.java", "line": 6,
    "severity": "warning", "confidence": 0.85,
    "title": "read can return null for a binary entry",
    "body": (
        "`text()` returns null when the entry holds binary data. The method returns "
        "that null as a plain `String`, and the Javadoc promises \"the current config "
        "text\". A caller such as the parser then fails on null instead of getting a "
        "clear error naming the entry. Return `Optional<String>`, or throw a typed "
        "exception when the value is null."
    ),
}

ACCOUNTS = """public class AccountService {
    public void provision(String name, AccountSpec spec) {
        // The account is confirmed absent first, so cleanup can only remove one this call created.
        if (admin.findAccount(name).isPresent()) {
            throw new AccountConflictException(name);
        }
        try {
            admin.createAccount(spec);
        } catch (AlreadyExistsException e) {
            throw new AccountConflictException(name);
        } catch (RuntimeException e) {
            removeCreatedAccount(name);   // delete by name
            throw e;
        }
    }
}"""
ACCOUNTS_UNGUARDED = """public class AccountService {
    public void provision(String name, AccountSpec spec) {
        try {
            admin.createAccount(spec);
        } catch (AlreadyExistsException e) {
            throw new AccountConflictException(name);
        } catch (RuntimeException e) {
            removeCreatedAccount(name);   // delete by name
            throw e;
        }
    }
}"""
ACCOUNTS_PATH = "src/main/java/accounts/AccountService.java"
CASE_TWO_DIFF = _new_file(ACCOUNTS_PATH, ACCOUNTS)
CASE_TWO_UNGUARDED_DIFF = _new_file(ACCOUNTS_PATH, ACCOUNTS_UNGUARDED)
CASE_TWO = {
    "file": ACCOUNTS_PATH, "line": 12, "severity": "warning", "confidence": 0.85,
    "title": "Compensating delete-by-name can remove an account this call never created",
    "body": (
        "On a timeout the call never learns whether it created the account. A "
        "pre-existing account with that name is then deleted unguarded. Only compensate "
        "when the create reply returned an id, and delete by that id."
    ),
}
CASE_TWO_UNGUARDED = {**CASE_TWO, "line": 8}


def _check(finding: dict, diff: str):
    out, reasons = apply_contained_claim_check([Finding(**finding)], parse_unified_diff(diff))
    return out[0], reasons[0]


class TestCaseOneNullReturnGuardedByCaller:
    def test_the_issue_case_is_lowered_with_the_guard_named(self):
        f, reason = _check(CASE_ONE, CASE_ONE_DIFF)
        assert reason == "null_return_guarded_by_caller"
        assert f.severity == "outofscope" and f.confidence == 0.85
        assert "`parser/src/main/java/config/parser/ConfigParser.java:5`" in f.body
        assert "`InvalidConfigException`" in f.body
        assert f.body.startswith(CASE_ONE["body"])

    def test_the_lowered_body_names_its_boundary_so_no_note_is_added(self):
        f, _ = _check(CASE_ONE, CASE_ONE_DIFF)
        assert not apply_containment_note([f])[0].body.endswith(CONTAINMENT_NOTE_SUFFIX)

    def test_a_caller_with_no_guard_keeps_the_warning(self):
        original = Finding(**CASE_ONE)
        out, reasons = apply_contained_claim_check(
            [original], parse_unified_diff(CASE_ONE_NO_GUARD_NO_WRITER_DIFF),
        )
        assert out[0] is original and reasons == [None]

    def test_a_guard_only_in_the_finding_own_file_does_not_count(self):
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE)
        f, reason = _check(CASE_ONE, diff)
        assert reason is None and f.severity == "warning"

    def test_a_guard_that_throws_npe_is_no_clear_error(self):
        npe = PARSER_GUARDED.replace("InvalidConfigException", "NullPointerException")
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "parser/src/main/java/config/parser/ConfigParser.java", npe,
        )
        assert _check(CASE_ONE, diff)[1] is None

    def test_an_unrelated_file_with_a_guard_does_not_count(self):
        other = PARSER_GUARDED.replace("ConfigParser", "MetricsSink")
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "metrics/MetricsSink.java", other,
        )
        assert _check(CASE_ONE, diff)[1] is None

    def test_a_call_site_ties_the_guarding_file(self):
        loader = """def load(store, config_id):
    text = store.read(config_id)
    if text is None:
        raise ConfigMissing(config_id)
    return text
"""
        finding = {**CASE_ONE, "body": CASE_ONE["body"].replace("such as the parser ", "")}
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "jobs/loader.py", loader,
        )
        f, reason = _check(finding, diff)
        assert reason == "null_return_guarded_by_caller" and "`jobs/loader.py:3`" in f.body


    def test_a_named_file_guarding_a_local_not_a_parameter_does_not_count(self):
        local = PARSER_GUARDED.replace(
            "public Config parse(String json) {",
            "public Config parse(String id) {\n        String json = cache.get(id);",
        )
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "parser/src/main/java/config/parser/ConfigParser.java", local,
        )
        assert _check(CASE_ONE, diff)[1] is None

    def test_a_file_named_only_by_a_word_the_own_file_shares_does_not_count(self):
        other = PARSER_GUARDED.replace("ConfigParser", "ConfigAudit")
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "audit/ConfigAudit.java", other,
        )
        assert _check(CASE_ONE, diff)[1] is None

    def test_a_call_whose_result_is_not_the_checked_value_does_not_count(self):
        loader = """def load(store, config_id, fallback):
    text = store.read(config_id)
    if fallback is None:
        raise ConfigMissing(config_id)
    return text
"""
        finding = {**CASE_ONE, "body": CASE_ONE["body"].replace("such as the parser ", "")}
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "jobs/loader.py", loader,
        )
        assert _check(finding, diff)[1] is None


    def test_the_call_window_counts_source_lines_not_added_lines(self):
        diff = (
            "diff --git a/jobs/loader.py b/jobs/loader.py\n"
            "--- a/jobs/loader.py\n+++ b/jobs/loader.py\n"
            "@@ -1,4 +1,8 @@\n"
            "+def load(store, config_id):\n"
            "+    text = store.read(config_id)\n"
            "     a = 1\n     b = 2\n     c = 3\n     d = 4\n"
            "+    if text is None:\n"
            "+        raise ConfigMissing(config_id)\n"
        )
        finding = {**CASE_ONE, "body": CASE_ONE["body"].replace("such as the parser ", "")}
        full = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + diff
        assert _check(finding, full)[1] is None
        context = "     a = 1\n     b = 2\n     c = 3\n     d = 4\n"
        near = full.replace(context, "").replace("@@ -1,4 +1,8 @@", "@@ -0,0 +1,4 @@")
        assert _check(finding, near)[1] == "null_return_guarded_by_caller"


    def test_a_call_result_reassigned_before_the_guard_does_not_count(self):
        loader = """def load(store, config_id):
    text = store.read(config_id)
    text = text or default_text()
    if text is None:
        raise ConfigMissing(config_id)
    return text
"""
        finding = {**CASE_ONE, "body": CASE_ONE["body"].replace("such as the parser ", "")}
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "jobs/loader.py", loader,
        )
        assert _check(finding, diff)[1] is None
        assert _check(finding, diff.replace("+    text = text or default_text()\n", "").replace(
            "@@ -0,0 +1,6 @@", "@@ -0,0 +1,5 @@"))[1] == "null_return_guarded_by_caller"

    def test_the_enclosing_header_may_be_an_unchanged_line(self):
        def parser_diff(header: str) -> str:
            return (
                "diff --git a/parser/ConfigParser.java b/parser/ConfigParser.java\n"
                "--- a/parser/ConfigParser.java\n+++ b/parser/ConfigParser.java\n"
                "@@ -1,4 +1,8 @@\n"
                "+    public Config load(String json) {\n"
                "+        return parse(json);\n"
                "+    }\n"
                f"     {header}\n"
                "+        if (json == null || json.isBlank()) {\n"
                '+            throw new InvalidConfigException("config is empty");\n'
                "+        }\n"
                "         return Config.from(json);\n"
            )
        store = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE)
        assert _check(CASE_ONE, store + parser_diff("public Config parse(String json) {"))[1] == (
            "null_return_guarded_by_caller"
        )
        assert _check(CASE_ONE, store + parser_diff("public Config parse(String id) {"))[1] is None


STORE_PATH = "store/src/main/java/config/store/ConfigStore.java"
PARSER_PATH = "parser/src/main/java/config/parser/ConfigParser.java"


def _writer_diff(writer: str, *extra: tuple[str, str]) -> str:
    out = _new_file(STORE_PATH, STORE) + _new_file(PARSER_PATH, PARSER_UNGUARDED)
    out += _new_file("writer/store.py", writer)
    for path, body in extra:
        out += _new_file(path, body)
    return out


class TestCaseOneWriterRulesOutNull:
    """The issue's writer: the PR's only writer always stores text, the caller has no guard."""

    def test_the_only_writer_storing_the_field_read_contains_the_claim(self):
        f, reason = _check(CASE_ONE, CASE_ONE_UNGUARDED_DIFF)
        assert reason == "null_return_ruled_out_by_writer"
        assert f.severity == "outofscope" and f.title == CASE_ONE["title"]
        assert "`writer/store.py:2`" in f.body and "`Text`" in f.body and "`text()`" in f.body
        assert "Writers outside this PR are not visible" in f.body

    def test_an_untyped_parameter_value_names_the_assumption(self):
        f, _ = _check(CASE_ONE, CASE_ONE_UNGUARDED_DIFF)
        assert (
            "Its `text` argument comes from callers outside this PR, so that value is "
            "assumed non-null, not shown to be."
        ) in f.body

    def test_a_local_value_states_no_parameter_assumption(self):
        writer = WRITER.replace("    client.put_value", "    body = render(config_id)\n    client.put_value").replace(
            "Text=text)", "Text=body)",
        )
        f, reason = _check(CASE_ONE, _writer_diff(writer))
        assert reason == "null_return_ruled_out_by_writer"
        assert "comes from callers outside this PR" not in f.body

    def test_the_writer_note_names_a_boundary(self):
        f, _ = _check(CASE_ONE, CASE_ONE_UNGUARDED_DIFF)
        assert not apply_containment_note([f])[0].body.endswith(CONTAINMENT_NOTE_SUFFIX)

    def test_the_caller_guard_still_wins_when_both_are_present(self):
        assert _check(CASE_ONE, CASE_ONE_DIFF)[1] == "null_return_guarded_by_caller"

    @pytest.mark.parametrize("writer", [
        "def publish(client, config_id, data):\n    client.put_value(Id=config_id, Binary=data)\n",
        "def publish(client, config_id, text):\n    client.put_value(Id=config_id, Text=None)\n",
        "def publish(client, config_id, text):\n    client.put_value(config_id, text)\n",
        "def publish(client, config_id, text):\n    client.put_secret(Id=config_id, Text=text)\n",
    ], ids=["other-field", "null-literal", "positional", "other-noun"])
    def test_a_writer_that_does_not_always_store_the_field_does_not_count(self, writer):
        assert _check(CASE_ONE, _writer_diff(writer))[1] is None

    def test_a_second_writer_anywhere_in_the_diff_does_not_count(self):
        second = "def migrate(client, config_id, blob):\n    client.putValue(Id=config_id, Binary=blob)\n"
        assert _check(CASE_ONE, _writer_diff(WRITER, ("jobs/migrate.py", second)))[1] is None
        same_field = "def seed(client, config_id):\n    client.put_value(Id=config_id, Text=DEFAULT)\n"
        assert _check(CASE_ONE, _writer_diff(WRITER, ("jobs/seed.py", same_field)))[1] is None

    @pytest.mark.parametrize("quoted", [
        "# client.put_value(Id=config_id, Text=text)",
        "// client.putValue(Id=config_id, Text=text)",
        " * client.putValue(Id=config_id, Text=text)",
        'log.info("client.put_value(Id=config_id, Text=text)")',
    ], ids=["hash-comment", "slash-comment", "javadoc", "string"])
    def test_a_write_call_in_a_comment_or_string_is_no_writer(self, quoted):
        writer = f"def publish(client, config_id, text):\n    {quoted}\n"
        assert _check(CASE_ONE, _writer_diff(writer))[1] is None

    @pytest.mark.parametrize("signature", [
        "def publish(client, config_id, text=None):",
        "def publish(client, config_id, text: str | None):",
        "def publish(client, config_id, text: Optional[str]):",
    ], ids=["none-default", "union-none", "optional"])
    def test_a_writer_value_the_file_shows_nullable_does_not_count(self, signature):
        writer = WRITER.replace("def publish(client, config_id, text):", signature)
        assert signature in writer
        assert _check(CASE_ONE, _writer_diff(writer))[1] is None

    @pytest.mark.parametrize("value", [
        "text if missing else None", "data.get('text')", "data['text']", "self.text", "None",
    ], ids=["ternary", "call", "subscript", "attribute", "null-literal"])
    def test_a_writer_value_that_is_an_expression_does_not_count(self, value):
        writer = WRITER.replace("Text=text)", f"Text={value})")
        assert f"Text={value})" in writer
        assert _check(CASE_ONE, _writer_diff(writer))[1] is None

    def test_a_bare_name_value_counts(self):
        writer = WRITER.replace("Text=text)", "Text=body)").replace(", text):", ", body):")
        assert "Text=body)" in writer
        assert _check(CASE_ONE, _writer_diff(writer))[1] == "null_return_ruled_out_by_writer"

    @pytest.mark.parametrize("assignment", [
        "text = None if not text else text",
        "text = fetch(config_id) if ready else None",
        "text = cache.get(config_id) or None",
    ], ids=["null-first", "null-last", "or-null"])
    def test_a_value_assigned_null_on_some_path_does_not_count(self, assignment):
        writer = WRITER.replace("    client.put_value", f"    {assignment}\n    client.put_value")
        assert assignment in writer
        assert _check(CASE_ONE, _writer_diff(writer))[1] is None

    def test_a_quoted_write_call_does_not_hide_the_real_writer(self):
        writer = WRITER + '    log.info("put_value(Id=x, Binary=b) was not called")\n'
        assert _check(CASE_ONE, _writer_diff(writer))[1] == "null_return_ruled_out_by_writer"

    @pytest.mark.parametrize("second", [
        "client.put_value(Id=f(x), Binary=b)",
        "client.putValue(Id=ids.next(prefix(x)), Binary=b)",
        "client.put_value(\n        Id=f(x),",
    ], ids=["nested", "deeply-nested", "spans-lines"])
    def test_a_second_writer_with_nested_arguments_still_counts(self, second):
        body = f"def migrate(client, x, b):\n    {second}\n"
        assert _check(CASE_ONE, _writer_diff(WRITER, ("jobs/migrate.py", body)))[1] is None

    def test_a_writer_with_nested_arguments_reads_its_named_field(self):
        writer = WRITER.replace("Id=config_id", "Id=key(config_id, 'v1')")
        assert "Id=key(config_id, 'v1'), Text=text" in writer
        assert _check(CASE_ONE, _writer_diff(writer))[1] == "null_return_ruled_out_by_writer"
        nested_value = WRITER.replace("Text=text", "Text=wrap(text, x)")
        assert _check(CASE_ONE, _writer_diff(nested_value))[1] is None

    def test_a_writer_on_an_unchanged_line_does_not_count(self):
        diff = _writer_diff("def unrelated():\n    pass\n").replace(
            "diff --git a/writer/store.py b/writer/store.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/writer/store.py\n@@ -0,0 +1,2 @@\n"
            "+def unrelated():\n+    pass\n",
            "diff --git a/writer/store.py b/writer/store.py\n"
            "--- a/writer/store.py\n+++ b/writer/store.py\n@@ -1,2 +1,3 @@\n"
            " def publish(client, config_id, text):\n"
            "     client.put_value(Id=config_id, Text=text)\n"
            "+# touched\n",
        )
        assert "     client.put_value" in diff
        assert _check(CASE_ONE, diff)[1] is None

    def test_an_anchor_that_is_not_a_getter_field_read_does_not_count(self):
        assert _check({**CASE_ONE, "line": 5}, CASE_ONE_UNGUARDED_DIFF)[1] is None

    def test_a_claim_without_a_null_return_does_not_count(self):
        finding = {**CASE_ONE, "title": "read is slow", "body": "`read` makes a network call per entry."}
        assert _check(finding, CASE_ONE_UNGUARDED_DIFF)[1] is None


class TestCaseTwoPreconditionRulesOutPreexisting:
    def test_the_issue_case_is_lowered_with_the_check_named(self):
        f, reason = _check(CASE_TWO, CASE_TWO_DIFF)
        assert reason == "precondition_rules_out_preexisting"
        assert f.severity == "outofscope"
        assert f"`{ACCOUNTS_PATH}:4`" in f.body and "race" in f.body

    def test_no_absence_check_keeps_the_warning(self):
        original = Finding(**CASE_TWO_UNGUARDED)
        out, reasons = apply_contained_claim_check([original], parse_unified_diff(CASE_TWO_UNGUARDED_DIFF))
        assert out[0] is original and reasons == [None]

    def test_a_check_on_another_identifier_does_not_count(self):
        diff = _new_file(ACCOUNTS_PATH, ACCOUNTS.replace("findAccount(name)", "findAccount(owner)"))
        assert _check(CASE_TWO, diff)[1] is None

    def test_a_check_in_another_function_does_not_count(self):
        split = ACCOUNTS.replace(
            "            throw new AccountConflictException(name);\n        }\n        try {",
            "            throw new AccountConflictException(name);\n        }\n    }\n"
            "    public void create(String name, AccountSpec spec) {\n        try {",
            1,
        )
        finding = {**CASE_TWO, "line": 14}
        assert "removeCreatedAccount" in split.splitlines()[13]
        assert _check(finding, _new_file(ACCOUNTS_PATH, split))[1] is None


    @pytest.mark.parametrize("negated", [
        "if (!admin.findAccount(name).isPresent()) {",
        "if (admin.findAccount(name).isEmpty()) {",
        "if (admin.findAccount(name) == null) {",
        "if (!admin.exists(name)) {",
    ])
    def test_a_check_that_exits_on_absence_does_not_count(self, negated):
        diff = _new_file(ACCOUNTS_PATH, ACCOUNTS.replace("if (admin.findAccount(name).isPresent()) {", negated))
        assert _check(CASE_TWO, diff)[1] is None

    def test_a_python_unless_style_check_does_not_count(self):
        body = """def provision(name, spec):
    if not admin.exists(name):
        return
    try:
        admin.create(spec)
    except Exception:
        remove_created(name)
        raise
"""
        finding = {**CASE_TWO, "file": "accounts.py", "line": 7}
        assert _check(finding, _new_file("accounts.py", body))[1] is None

    def test_a_positive_python_check_counts(self):
        body = """def provision(name, spec):
    if admin.exists(name):
        raise AccountConflict(name)
    try:
        admin.create(spec)
    except Exception:
        remove_created(name)
        raise
"""
        finding = {**CASE_TWO, "file": "accounts.py", "line": 7}
        assert _check(finding, _new_file("accounts.py", body))[1] == "precondition_rules_out_preexisting"


    def test_an_exit_after_the_check_block_does_not_count(self):
        diff = _new_file(ACCOUNTS_PATH, ACCOUNTS.replace(
            "            throw new AccountConflictException(name);\n        }\n        try {",
            "            log.info(name);\n        }\n        try {",
            1,
        ))
        assert _check(CASE_TWO, diff)[1] is None

    def test_a_null_check_whose_block_does_not_throw_does_not_count(self):
        logged = PARSER_GUARDED.replace(
            '            throw new InvalidConfigException("config is empty");\n        }',
            '            log.warn("empty");\n        }\n        throw new InvalidConfigException("x");',
        )
        assert logged != PARSER_GUARDED
        diff = _new_file("store/src/main/java/config/store/ConfigStore.java", STORE) + _new_file(
            "parser/src/main/java/config/parser/ConfigParser.java", logged,
        )
        assert _check(CASE_ONE, diff)[1] is None


    @pytest.mark.parametrize("nested", [
        "            if (strict) {\n                throw new AccountConflictException(name);\n            }",
        "            if (strict) throw new AccountConflictException(name);",
    ])
    def test_an_exit_that_is_itself_conditional_does_not_count(self, nested):
        diff = _new_file(ACCOUNTS_PATH, ACCOUNTS.replace(
            "            throw new AccountConflictException(name);\n        }\n        try {",
            nested + "\n        }\n        try {",
            1,
        ))
        line = CASE_TWO["line"] + nested.count("\n")
        source = ACCOUNTS.replace(
            "            throw new AccountConflictException(name);\n        }\n        try {",
            nested + "\n        }\n        try {",
            1,
        )
        assert "removeCreatedAccount(name)" in source.splitlines()[line - 1]
        assert _check({**CASE_TWO, "line": line}, diff)[1] is None


class TestPassThrough:
    @pytest.mark.parametrize("severity", ["outofscope", "spec"])
    def test_only_errors_and_warnings_are_lowered(self, severity):
        original = Finding(**{**CASE_ONE, "severity": severity})
        out, _ = apply_contained_claim_check([original], parse_unified_diff(CASE_ONE_DIFF))
        assert out[0] is original

    def test_a_dropped_finding_is_untouched(self):
        original = Finding(**CASE_TWO, drop_reason="hedged")
        out, _ = apply_contained_claim_check([original], parse_unified_diff(CASE_TWO_DIFF))
        assert out[0] is original

    def test_idempotent(self):
        once, _ = apply_contained_claim_check([Finding(**CASE_TWO)], parse_unified_diff(CASE_TWO_DIFF))
        twice, reasons = apply_contained_claim_check(
            [Finding(**{**CASE_TWO, "body": once[0].body})], parse_unified_diff(CASE_TWO_DIFF),
        )
        assert reasons == [None] and twice[0].body.count("Contained:") == 1

    def test_reasons_are_the_published_names(self):
        assert CONTAINED_REASONS == (
            "null_return_guarded_by_caller",
            "precondition_rules_out_preexisting",
            "null_return_ruled_out_by_writer",
        )


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheOrchestrator:
    def test_case_one_across_chunks_posts_as_minor(self):
        llm = FakeLLM({CASE_ONE["file"]: [CASE_ONE]})
        res = orchestrator.orchestrate_review(
            FakeForge(diff=CASE_ONE_DIFF), REF, llm, post=False, max_workers=1,
        )
        (f,) = res["findings_active"]
        assert f.severity == "outofscope" and "ConfigParser.java:5" in f.body
        assert not f.body.endswith(CONTAINMENT_NOTE_SUFFIX)
        assert res["contained_claims"] == {"null_return_guarded_by_caller": 1}

    def test_case_one_writer_only_posts_as_minor(self):
        llm = FakeLLM({CASE_ONE["file"]: [CASE_ONE]})
        res = orchestrator.orchestrate_review(
            FakeForge(diff=CASE_ONE_UNGUARDED_DIFF), REF, llm, post=False, max_workers=1,
        )
        (f,) = res["findings_active"]
        assert f.severity == "outofscope" and "writer/store.py:2" in f.body
        assert res["contained_claims"] == {"null_return_ruled_out_by_writer": 1}

    def test_case_two_posts_as_minor(self):
        llm = FakeLLM({ACCOUNTS_PATH: [CASE_TWO]})
        res = orchestrator.orchestrate_review(
            FakeForge(diff=CASE_TWO_DIFF), REF, llm, post=False, max_workers=1,
        )
        (f,) = res["findings_active"]
        assert f.severity == "outofscope" and f"{ACCOUNTS_PATH}:4" in f.body
        assert res["contained_claims"] == {"precondition_rules_out_preexisting": 1}

    def test_an_unguarded_case_keeps_its_warning_and_no_record_key(self):
        llm = FakeLLM({ACCOUNTS_PATH: [CASE_TWO_UNGUARDED]})
        res = orchestrator.orchestrate_review(
            FakeForge(diff=CASE_TWO_UNGUARDED_DIFF), REF, llm, post=False, max_workers=1,
        )
        (f,) = res["findings_active"]
        assert f.severity == "warning"
        assert "contained_claims" not in res
