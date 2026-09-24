"""Unit tests for the contract-trigger half of :mod:`prxref.repo_contracts`.

Covers :func:`contract_triggers` (routes, tables, names and operation ids on
added lines), :func:`select_contract_files` and :func:`literal_contract_paths`,
:func:`earlier_migrations`, the :func:`contract_excerpts` dispatch, and the
per-chunk :func:`contract_entries`. Everything is pure: the only file reads are
of ``tests/fixtures/issue17/repo/`` through a tiny reader defined here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import config
from prxref.repo_context import ContextEntry, language_of, referenced_names
from prxref.repo_contracts import (
    MAX_CONTRACT_LINES,
    MAX_EARLIER_MIGRATIONS,
    MAX_SPEC_FILES,
    ContractTriggers,
    Excerpt,
    contract_entries,
    contract_excerpts,
    contract_triggers,
    earlier_migrations,
    json_schema_excerpts,
    liquibase_excerpts,
    literal_contract_paths,
    openapi_json_excerpts,
    openapi_yaml_excerpts,
    select_contract_files,
    sql_excerpts,
)
from prxref.triage import build_chunks, parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
SPEC = "api/openapi/connectors.yaml"
MIGRATION_001 = "db/changelog/001-create-connectors.sql"
MIGRATION_002 = "db/changelog/002-create-idempotency-keys.sql"
MIGRATION_003 = "db/changelog/003-idempotency-unique.sql"
CONNECTOR_SERVICE = "src/main/java/com/acme/connectors/ConnectorService.java"
TRANSPORT_CONFIG = "src/main/java/com/acme/connectors/TransportConfig.java"

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"


def fixture_reader(log: list[str]):
    """A reader over the fixture repository that records every path asked for."""

    def read(path: str) -> str | None:
        log.append(path)
        target = REPO / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    return read


def dict_reader(files: dict[str, str | None], log: list[str]):
    """A reader over an in-memory repository that records every path asked for."""

    def read(path: str) -> str | None:
        log.append(path)
        return files.get(path)

    return read


def stand_in(path: str, added: list[str]) -> SimpleNamespace:
    """A FileDiff stand-in whose one hunk adds ``added``."""
    lines = [SimpleNamespace(kind="+", text=text, new_line=i + 1) for i, text in enumerate(added)]
    return SimpleNamespace(path=path, hunks=[SimpleNamespace(lines=lines)])


def routes(added: list[str], path: str = "src/Api.java", text: str | None = None) -> tuple[str, ...]:
    return contract_triggers(path, added, text=text).routes


def tables(added: list[str], path: str = "db/V1__x.sql") -> tuple[str, ...]:
    return contract_triggers(path, added).tables


def summary(entries: list[ContextEntry]) -> list[tuple[str, int, str]]:
    return [(entry.path, entry.line, entry.symbol) for entry in entries]


@pytest.fixture(scope="module")
def world():
    """The fixture diff parsed and chunked one file per chunk, plus its contract paths."""
    files = parse_unified_diff((FIXTURE / "pr.diff").read_text(encoding="utf-8"))
    chunks = build_chunks(files, max_files_per_chunk=1)
    listing = sorted(p.relative_to(REPO).as_posix() for p in REPO.rglob("*") if p.is_file())
    contract_paths = select_contract_files(
        config._DEFAULTS["context_contract_globs"],
        listing=listing,
        diff_paths=[f.path for f in files if f.status != "removed"],
    )
    by_path = {chunk[0].path: chunk for chunk in chunks}
    return SimpleNamespace(chunks=chunks, by_path=by_path, contract_paths=contract_paths)


class TestIssue17Fixture:
    """The fixture end to end: parse, chunk one file per chunk, select, read, excerpt."""

    def run(self, world, path: str) -> tuple[list[ContextEntry], list[str]]:
        log: list[str] = []
        entries = contract_entries(world.by_path[path], contract_paths=world.contract_paths, read=fixture_reader(log))
        return entries, log

    def test_three_single_file_chunks(self, world):
        assert [[f.path for f in chunk] for chunk in world.chunks] == [
            [CONNECTOR_SERVICE],
            [TRANSPORT_CONFIG],
            [MIGRATION_003],
        ]

    def test_default_globs_select_the_spec_and_every_migration(self, world):
        assert world.contract_paths == [SPEC, MIGRATION_001, MIGRATION_002, MIGRATION_003]

    def test_migration_chunk_triggers_and_earlier_migrations(self, world):
        chunk_file = world.by_path[MIGRATION_003][0]
        added = [line.text for hunk in chunk_file.hunks for line in hunk.lines if line.kind == "+"]
        triggers = contract_triggers(MIGRATION_003, added)
        assert triggers.tables == ("idempotency_keys",)
        assert triggers.routes == ()
        assert "ux_idempotency_keys" not in triggers.tables
        assert earlier_migrations(MIGRATION_003, world.contract_paths) == [MIGRATION_001, MIGRATION_002]

    def test_migration_chunk_gets_the_create_table_and_the_idempotency_schema(self, world):
        entries, log = self.run(world, MIGRATION_003)
        assert summary(entries) == [
            (SPEC, 50, "IdempotencyKey"),
            (MIGRATION_002, 4, "idempotency_keys"),
        ]
        table = entries[1]
        assert table.text.startswith("CREATE TABLE idempotency_keys (")
        assert "connector_id" in table.text
        assert "(tenant, connector, key)" in entries[0].text
        assert log == [MIGRATION_001, MIGRATION_002, SPEC]
        assert all(entry.path not in (MIGRATION_001, MIGRATION_003) for entry in entries)

    def test_connector_service_chunk_gets_the_path_item_and_both_schemas(self, world):
        entries, log = self.run(world, CONNECTOR_SERVICE)
        assert summary(entries) == [
            (SPEC, 6, "/connectors/{connectorId}/transports"),
            (SPEC, 31, "TransportConfig"),
            (SPEC, 41, "CreateTransportRequest"),
        ]
        path_item = entries[0].text.split("\n")
        assert len(path_item) == 23
        assert "    operationId: createTransport" in path_item
        assert "mutually exclusive" in entries[1].text
        assert log == [CONNECTOR_SERVICE, SPEC]

    def test_connector_service_triggers(self, world):
        chunk_file = world.by_path[CONNECTOR_SERVICE][0]
        added = [line.text for hunk in chunk_file.hunks for line in hunk.lines if line.kind == "+"]
        text = (REPO / CONNECTOR_SERVICE).read_text(encoding="utf-8")
        triggers = contract_triggers(CONNECTOR_SERVICE, added, text=text)
        assert triggers.routes == ("/connectors/{connectorId}/transports",)
        assert triggers.tables == ()
        assert "createTransport" in triggers.operation_ids
        assert {"TransportConfig", "CreateTransportRequest"} <= set(triggers.names)

    def test_transport_config_chunk_gets_its_schema_only(self, world):
        entries, log = self.run(world, TRANSPORT_CONFIG)
        assert summary(entries) == [(SPEC, 31, "TransportConfig")]
        assert "mutually exclusive" in entries[0].text
        assert log == [SPEC]
        chunk_file = world.by_path[TRANSPORT_CONFIG][0]
        added = [line.text for hunk in chunk_file.hunks for line in hunk.lines if line.kind == "+"]
        assert contract_triggers(TRANSPORT_CONFIG, added) == ContractTriggers(
            routes=(), tables=(), names=("TransportConfig",), operation_ids=("IllegalArgumentException",)
        )

    def test_every_entry_is_a_contract(self, world):
        for chunk in world.chunks:
            entries, _ = self.run(world, chunk[0].path)
            assert entries
            assert {(entry.kind, entry.reason) for entry in entries} == {("contract", "contract")}


class TestRoutes:
    def test_spring_positional(self):
        assert routes(['    @GetMapping("/orders/{id}")']) == ("/orders/{id}",)

    def test_spring_value(self):
        assert routes(['@PostMapping(value = "/orders", produces = "application/json")']) == ("/orders",)

    def test_spring_path(self):
        assert routes(['@PutMapping(consumes = "text/plain", path = "/orders/{id}")']) == ("/orders/{id}",)

    def test_spring_array_forms(self):
        assert routes(['@DeleteMapping({"/a", "/b"})']) == ("/a", "/b")
        assert routes(['@RequestMapping(path = {"/c", "/d"}, method = RequestMethod.GET)']) == ("/c", "/d")

    def test_spring_ignores_produces_consumes_name_headers_and_params(self):
        line = (
            '@PatchMapping(produces = {"application/json"}, consumes = "text/plain", name = "patch",'
            ' headers = "X-A=1", params = "p=1", value = "/orders")'
        )
        assert routes([line]) == ("/orders",)
        assert routes(['@GetMapping(produces = "application/json")']) == ()

    def test_spring_annotation_wrapped_across_added_lines(self):
        assert routes(["@GetMapping(", '    value = "/wrapped",', '    produces = "application/json")']) == (
            "/wrapped",
        )

    def test_jax_rs_path(self):
        assert routes(['    @Path("/items/{id}")', '    public Item get(@PathParam("id") String id) {']) == (
            "/items/{id}",
        )

    def test_fastapi(self):
        added = ['@router.get("/items/{item_id}", response_model=Item)', "@app.api_route('/health', methods=['GET'])"]
        assert routes(added, path="app/api.py") == ("/items/{item_id}", "/health")

    def test_flask(self):
        added = ['@bp.route("/users/<int:user_id>", methods=["POST"])', "@app.post('/login')"]
        assert routes(added, path="app/views.py") == ("/users/<int:user_id>", "/login")

    def test_express_needs_a_leading_slash(self):
        added = [
            "router.post('/orders/:id', handler);",
            'app.get("orders", handler);',
            "app.use(`/static`, serve);",
            "const value = cache.get(key);",
        ]
        assert routes(added, path="web/server.js") == ("/orders/:id", "/static")

    def test_empty_literals_are_dropped(self):
        assert routes(['@router.get("")'], path="app/api.py") == ()

    def test_first_appearance_order_and_dedup(self):
        added = ['@GetMapping("/b")', '@GetMapping("/a")', '@PostMapping("/b")']
        assert routes(added) == ("/b", "/a")


SPRING_CONTROLLER = """package com.acme.orders;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1")
public class OrderController {

    @RequestMapping("/not-a-prefix")
    @GetMapping("/orders")
    public List<Order> list() {
        return List.of();
    }
}
"""

WRAPPED_SPRING_CONTROLLER = """package com.acme.orders;

@RestController
@RequestMapping(
    value = {"/api", "/legacy"},
    produces = "application/json")
public class OrderController {
}
"""


class TestRoutePrefixes:
    def test_class_level_spring_prefix_is_joined(self):
        assert routes(['    @GetMapping("/orders")'], text=SPRING_CONTROLLER) == ("/orders", "/api/v1/orders")

    def test_no_text_means_no_prefix(self):
        assert routes(['    @GetMapping("/orders")']) == ("/orders",)

    def test_wrapped_class_annotation_gives_its_first_path(self):
        assert routes(['@GetMapping("/orders")'], text=WRAPPED_SPRING_CONTROLLER) == ("/orders", "/api/orders")

    def test_segments_without_a_slash_are_joined_with_one(self):
        text = '@RestController\n@RequestMapping("api/")\npublic class C {\n}\n'
        assert routes(['@GetMapping("orders")'], text=text) == ("orders", "api/orders")

    def test_a_method_level_request_mapping_is_not_a_prefix(self):
        text = 'public class C {\n    @RequestMapping("/method")\n    public void m() {}\n}\n'
        assert routes(['@GetMapping("/x")'], text=text) == ("/x",)

    def test_class_level_jax_rs_path(self):
        text = '@Path("/connectors")\n@Produces(MediaType.APPLICATION_JSON)\npublic class ConnectorResource {\n}\n'
        assert routes(['    @Path("{id}/transports")'], text=text) == (
            "{id}/transports",
            "/connectors/{id}/transports",
        )

    def test_same_line_annotation_counts(self):
        text = '@RequestMapping("/api") public interface OrdersApi {\n}\n'
        assert routes(['@GetMapping("/orders")'], text=text) == ("/orders", "/api/orders")

    def test_flask_url_prefix(self):
        text = 'bp = Blueprint("users", __name__, url_prefix="/users")\n\n\n@bp.route("/<user_id>")\ndef show(uid):\n'
        assert routes(['@bp.route("/<user_id>")'], path="app/users.py", text=text) == (
            "/<user_id>",
            "/users/<user_id>",
        )

    def test_fastapi_prefix(self):
        text = 'router = APIRouter(prefix="/items", tags=["items"])\n\n@router.get("/{item_id}")\n'
        assert routes(['@router.get("/{item_id}")'], path="app/items.py", text=text) == (
            "/{item_id}",
            "/items/{item_id}",
        )

    def test_only_the_first_prefix_of_each_kind(self):
        text = (
            'a = Blueprint("a", __name__, url_prefix="/first")\n'
            'b = Blueprint("b", __name__, url_prefix="/second")\n'
            'r = APIRouter(prefix="/router")\n'
        )
        assert routes(['@a.get("/x")'], path="app/x.py", text=text) == ("/x", "/first/x", "/router/x")

    def test_text_without_a_route_on_the_added_lines_adds_nothing(self):
        assert routes(["return List.of();"], text=SPRING_CONTROLLER) == ()


class TestTables:
    def test_create_table_with_schema_prefix_and_quoting(self):
        assert tables(['CREATE TABLE IF NOT EXISTS "public"."Orders" (']) == ("Orders",)
        assert tables(["create table `shop`.`line_items` ("]) == ("line_items",)
        assert tables(["CREATE TABLE [dbo].[Invoices] ("]) == ("Invoices",)

    def test_alter_table(self):
        assert tables(["ALTER TABLE ONLY public.accounts ADD COLUMN region text;"]) == ("accounts",)

    def test_index_on_gives_the_table_never_the_index_name(self):
        found = tables(["CREATE UNIQUE INDEX ux_idempotency_keys ON idempotency_keys (tenant_id, key);"])
        assert found == ("idempotency_keys",)

    def test_index_wrapped_across_added_lines(self):
        assert tables(["CREATE INDEX ix_orders_tenant", "    ON orders (tenant_id);"]) == ("orders",)

    def test_references_is_a_foreign_key_target(self):
        added = [
            "    connector_id VARCHAR(36) REFERENCES connectors (id),",
            "    tenant_id VARCHAR(36) REFERENCES tenants ON DELETE CASCADE",
        ]
        assert tables(added) == ("connectors", "tenants")

    def test_prose_references_is_not_a_table(self):
        assert tables(["# this references the old layout"], path="app/models.py") == ()

    def test_liquibase_xml(self):
        added = [
            '<createIndex indexName="ux_idempotency_keys" tableName="idempotency_keys">',
            '<addForeignKeyConstraint baseTableName="orders" referencedTableName="tenants"/>',
        ]
        assert tables(added, path="db/changelog/004.xml") == ("idempotency_keys", "orders", "tenants")

    def test_liquibase_yaml(self):
        added = ["    - createIndex:", "        tableName: idempotency_keys", "        baseTableName: 'orders'"]
        assert tables(added, path="db/changelog/004.yaml") == ("idempotency_keys", "orders")

    def test_liquibase_json(self):
        added = ['          "tableName": "idempotency_keys",', '          "referencedTableName" : "tenants"']
        assert tables(added, path="db/changelog/004.json") == ("idempotency_keys", "tenants")

    def test_sql_inside_a_java_string(self):
        added = ['        jdbc.execute("ALTER TABLE accounts ADD COLUMN region text");']
        assert tables(added, path="src/main/java/Migrate.java") == ("accounts",)

    def test_first_appearance_order_and_dedup(self):
        added = ["ALTER TABLE b ADD x int;", "CREATE TABLE a (id int);", "ALTER TABLE b ADD y int;"]
        assert tables(added) == ("b", "a")


class TestNamesAndOperationIds:
    def test_java_line(self):
        path = "src/main/java/com/acme/OrderService.java"
        added = [
            "    public Order createOrder(CreateOrderRequest request) {",
            "        for(int i = 0; i < n; i++) save(i);",
        ]
        triggers = contract_triggers(path, added)
        assert triggers.names == tuple(referenced_names(added, language_of(path)))
        assert triggers.names == ("Order", "CreateOrderRequest")
        assert triggers.operation_ids == ("createOrder", "save")

    def test_python_line(self):
        path = "app/orders.py"
        added = ["    result = create_order(payload); print(len(result)); obj.save (x)"]
        triggers = contract_triggers(path, added)
        assert triggers.names == tuple(referenced_names(added, language_of(path)))
        assert triggers.operation_ids == ("create_order",)

    def test_nothing_on_blank_lines(self):
        assert contract_triggers("x.txt", ["", "}"]) == ContractTriggers()


class TestSelection:
    GLOBS = config._DEFAULTS["context_contract_globs"]

    def test_a_glob_match_from_the_listing(self):
        listing = ["api/openapi.yaml", "src/Order.java", "db/migration/V1__init.sql"]
        assert select_contract_files(self.GLOBS, listing=listing, diff_paths=[]) == [
            "api/openapi.yaml",
            "db/migration/V1__init.sql",
        ]

    def test_a_diff_only_path(self):
        assert select_contract_files(self.GLOBS, listing=["README.md"], diff_paths=["db/migration/V2__x.sql"]) == [
            "db/migration/V2__x.sql"
        ]

    def test_a_literal_glob_absent_from_the_listing_is_included(self):
        globs = ["docs/api/contract.yaml", "**/openapi*.y*ml"]
        assert select_contract_files(globs, listing=["a/openapi.yml"], diff_paths=[]) == [
            "a/openapi.yml",
            "docs/api/contract.yaml",
        ]

    def test_a_negation_vetoes_listing_paths_and_literals(self):
        globs = ["**/migrations/**", "migrations/archive/old.sql", "!**/archive/**"]
        listing = ["migrations/001.sql", "migrations/archive/000.sql"]
        assert select_contract_files(globs, listing=listing, diff_paths=[]) == ["migrations/001.sql"]

    def test_listing_none_keeps_diff_paths_and_literals(self):
        globs = ["api/spec.json", "**/db/changelog/**"]
        assert select_contract_files(globs, listing=None, diff_paths=["db/changelog/2.sql", "src/A.java"]) == [
            "api/spec.json",
            "db/changelog/2.sql",
        ]

    def test_sorted_and_deduplicated(self):
        globs = ["**/*.schema.json", "b.schema.json"]
        listing = ["z/c.schema.json", "b.schema.json", "a.schema.json"]
        assert select_contract_files(globs, listing=listing, diff_paths=["a.schema.json", "z/c.schema.json"]) == [
            "a.schema.json",
            "b.schema.json",
            "z/c.schema.json",
        ]

    def test_literal_contract_paths(self):
        globs = ["a.yaml", "b/*.yaml", "!c.yaml", "a.yaml", "", "  ", "d?.yaml", "e[1].yaml", "f.json", "c.yaml"]
        assert literal_contract_paths(globs) == ["a.yaml", "f.json"]

    def test_the_default_globs_hold_no_literal(self):
        assert literal_contract_paths(self.GLOBS) == []


class TestEarlierMigrations:
    def test_natural_sort_puts_v9_before_v10(self):
        paths = ["db/migration/V10__b.sql", "db/migration/V9__a.sql", "db/migration/V11__c.sql"]
        assert earlier_migrations("db/migration/V11__c.sql", paths) == [
            "db/migration/V9__a.sql",
            "db/migration/V10__b.sql",
        ]

    def test_at_most_the_nearest_four(self):
        paths = [f"db/migration/V{i}__step.sql" for i in range(1, 9)]
        assert MAX_EARLIER_MIGRATIONS == 4
        assert earlier_migrations("db/migration/V8__step.sql", paths) == [
            f"db/migration/V{i}__step.sql" for i in (4, 5, 6, 7)
        ]

    def test_other_directories_other_extensions_later_files_and_itself_are_excluded(self):
        paths = [
            "db/changelog/001.sql",
            "db/changelog/002.md",
            "db/changelog/003.java",
            "db/changelog/nested/001.sql",
            "db/other/001.sql",
            "db/changelog/004.sql",
            "db/changelog/005.sql",
        ]
        assert earlier_migrations("db/changelog/004.sql", paths) == ["db/changelog/001.sql"]

    def test_every_migration_extension_counts(self):
        paths = ["m/1.sql", "m/2.XML", "m/3.yaml", "m/4.yml", "m/5.json", "m/6.sql"]
        assert earlier_migrations("m/6.sql", paths) == ["m/2.XML", "m/3.yaml", "m/4.yml", "m/5.json"]


OPENAPI_YAML = """openapi: 3.0.3
paths:
  /orders:
    post:
      operationId: createOrder
components:
  schemas:
    Order:
      type: object
"""

LIQUIBASE_YAML = """databaseChangeLog:
  - changeSet:
      id: 1
      changes:
        - createTable:
            tableName: orders
"""

OPENAPI_JSON = (
    '{"openapi":"3.0.0","paths":{"/orders":{"post":{"operationId":"createOrder"}}},'
    '"components":{"schemas":{"Order":{"type":"object"}}}}'
)

LIQUIBASE_JSON = (
    '{"databaseChangeLog": [{"changeSet": {"id": "1", "changes": [{"createTable": {"tableName": "orders"}}]}}]}'
)

SCHEMA_JSON = '{\n  "title": "Order",\n  "type": "object",\n  "properties": {"openapi": {"type": "string"}}\n}\n'

TRIGGERS = ContractTriggers(
    routes=("/orders",), tables=("orders",), names=("Order", "orders"), operation_ids=("createOrder",)
)


class TestDispatch:
    def test_sql(self):
        text = "CREATE TABLE orders (id int);\nCREATE TABLE other (id int);\n"
        assert contract_excerpts("db/V1__init.SQL", text, TRIGGERS) == sql_excerpts(text, tables=("orders",))
        assert [e.symbol for e in contract_excerpts("db/V1__init.sql", text, TRIGGERS)] == ["orders"]

    def test_xml(self):
        text = '<databaseChangeLog>\n<changeSet id="1" author="a">\n<createTable tableName="orders"/>\n</changeSet>\n'
        found = contract_excerpts("db/changelog/1.Xml", text, TRIGGERS)
        assert found == liquibase_excerpts(text, tables=("orders",))
        assert [e.line for e in found] == [2]

    def test_openapi_yaml(self):
        found = contract_excerpts("api/openapi.yaml", OPENAPI_YAML, TRIGGERS)
        assert found == openapi_yaml_excerpts(
            OPENAPI_YAML, routes=("/orders",), operation_ids=("createOrder",), schemas=("Order", "orders")
        )
        assert [(e.line, e.symbol) for e in found] == [(3, "/orders"), (8, "Order")]

    def test_swagger_yml_with_a_bom(self):
        text = '\N{ZERO WIDTH NO-BREAK SPACE}swagger: "2.0"\ndefinitions:\n  Order:\n    type: object\n'
        assert [(e.line, e.symbol) for e in contract_excerpts("api/swagger.yml", text, TRIGGERS)] == [(3, "Order")]

    def test_liquibase_yaml(self):
        found = contract_excerpts("db/changelog/1.yaml", LIQUIBASE_YAML, TRIGGERS)
        assert found == liquibase_excerpts(LIQUIBASE_YAML, tables=("orders",))
        assert [e.line for e in found] == [2]

    def test_fragment_matched_by_stem(self):
        text = "type: object\nproperties:\n  id:\n    type: string\n\n"
        found = contract_excerpts("api/openapi/schemas/Order.yaml", text, TRIGGERS)
        assert found == [Excerpt(1, "Order", "type: object\nproperties:\n  id:\n    type: string")]

    def test_fragment_matched_by_a_table_through_normalize_name(self):
        triggers = ContractTriggers(tables=("idempotency_keys",))
        found = contract_excerpts("api/schemas/idempotency-key.v2.yml", "type: object\n", triggers)
        assert found == [Excerpt(1, "idempotency-key", "type: object")]

    def test_fragment_not_matched(self):
        assert contract_excerpts("api/openapi/schemas/Invoice.yaml", "type: object\n", TRIGGERS) == []

    def test_fragment_is_capped_like_every_excerpt(self):
        text = "\n".join(f"line{i}: x" for i in range(60))
        (excerpt,) = contract_excerpts("api/schemas/Order.yaml", text, TRIGGERS)
        lines = excerpt.text.split("\n")
        assert len(lines) == MAX_CONTRACT_LINES
        assert lines[-1] == f"{ELLIPSIS} 21 more lines"

    def test_json_openapi(self):
        found = contract_excerpts("api/openapi.json", OPENAPI_JSON, TRIGGERS)
        assert found == openapi_json_excerpts(
            OPENAPI_JSON, routes=("/orders",), operation_ids=("createOrder",), schemas=("Order", "orders")
        )
        assert [e.symbol for e in found] == ["/orders", "Order"]

    def test_json_liquibase(self):
        found = contract_excerpts("db/changelog/1.json", LIQUIBASE_JSON, TRIGGERS)
        assert found == liquibase_excerpts(LIQUIBASE_JSON, tables=("orders",))
        assert [e.symbol for e in found] == ["orders"]

    def test_json_schema_even_with_a_nested_openapi_key(self):
        found = contract_excerpts("schemas/order.schema.json", SCHEMA_JSON, TRIGGERS)
        assert found == json_schema_excerpts(SCHEMA_JSON, names=("Order", "orders"))
        assert [(e.line, e.symbol) for e in found] == [(1, "Order")]

    def test_json_text_in_a_yaml_file_takes_the_json_branch(self):
        found = contract_excerpts("api/openapi.yaml", OPENAPI_JSON, TRIGGERS)
        assert [e.symbol for e in found] == ["/orders", "Order"]

    def test_unknown_extensions(self):
        assert contract_excerpts("api/openapi.md", OPENAPI_YAML, TRIGGERS) == []
        assert contract_excerpts("src/Order.java", "public class Order {}", TRIGGERS) == []


ORDER_SERVICE = "src/main/java/com/acme/OrderService.java"
ORDER_LINE = "        Order order = repository.find(id);"


class TestContractEntries:
    def test_read_none_gives_nothing(self):
        chunk = [stand_in(ORDER_SERVICE, [ORDER_LINE])]
        assert contract_entries(chunk, contract_paths=["api/openapi.yaml"], read=None) == []

    def test_empty_triggers_read_nothing(self):
        log: list[str] = []
        chunk = [stand_in("notes.txt", ["", "}", "  "])]
        found = contract_entries(chunk, contract_paths=["api/openapi.yaml", "db/1.sql"], read=dict_reader({}, log))
        assert found == []
        assert log == []

    def test_a_contract_file_in_the_chunk_is_not_read(self):
        log: list[str] = []
        chunk = [stand_in("api/openapi.yaml", ["  operationId: createOrder"]), stand_in(ORDER_SERVICE, [ORDER_LINE])]
        files = {"api/openapi.yaml": OPENAPI_YAML, "api/schemas/Order.yaml": "type: object\n"}
        found = contract_entries(chunk, contract_paths=sorted(files), read=dict_reader(files, log))
        assert log == ["api/schemas/Order.yaml"]
        assert summary(found) == [("api/schemas/Order.yaml", 1, "Order")]

    def test_max_spec_files_keeps_the_six_best_ranked(self):
        candidates = [
            "a/openapi.yaml",
            "b/Order.yaml",
            "c/swagger.json",
            "d/thing.schema.json",
            "e/aaa.yaml",
            "f/bbb.yaml",
            "g/ccc.yml",
            "z/priority.yaml",
        ]
        log: list[str] = []
        found = contract_entries(
            [stand_in(ORDER_SERVICE, [ORDER_LINE])],
            contract_paths=[*candidates, "db/changelog/1.sql", "db/changelog/2.xml"],
            read=dict_reader({}, log),
            priority=["z/priority.yaml", "not/a/contract.yaml"],
        )
        assert MAX_SPEC_FILES == 6
        assert found == []
        assert log == [
            "z/priority.yaml",
            "b/Order.yaml",
            "a/openapi.yaml",
            "c/swagger.json",
            "d/thing.schema.json",
            "e/aaa.yaml",
        ]

    def test_a_none_read_is_skipped(self):
        log: list[str] = []
        files = {"api/schemas/Order.yaml": "type: object\n", "api/openapi.yaml": None}
        found = contract_entries(
            [stand_in(ORDER_SERVICE, [ORDER_LINE])], contract_paths=sorted(files), read=dict_reader(files, log)
        )
        assert log == ["api/schemas/Order.yaml", "api/openapi.yaml"]
        assert summary(found) == [("api/schemas/Order.yaml", 1, "Order")]

    def test_order_by_path_and_line_and_dedup_on_both(self):
        files = {"b/openapi.json": OPENAPI_JSON, "a/openapi.yaml": OPENAPI_YAML}
        chunk = [stand_in("src/orders.js", ["router.post('/orders', createOrder);", "const o = new Order();"])]
        found = contract_entries(chunk, contract_paths=sorted(files), read=dict_reader(files, []))
        assert summary(found) == [
            ("a/openapi.yaml", 3, "/orders"),
            ("a/openapi.yaml", 8, "Order"),
            ("b/openapi.json", 1, "/orders"),
        ]

    def test_triggers_merge_across_the_chunk_files(self):
        spec = "openapi: 3.0.3\npaths:\n  /orders:\n    post: {}\ncomponents:\n  schemas:\n    Invoice:\n      type: x"
        files = {"a/openapi.yaml": spec}
        chunk = [
            stand_in("src/a.js", ["router.post('/orders', h);"]),
            stand_in("src/main/java/com/acme/Billing.java", ["        Invoice invoice = billing.find(id);"]),
        ]
        found = contract_entries(chunk, contract_paths=sorted(files), read=dict_reader(files, []))
        assert summary(found) == [("a/openapi.yaml", 3, "/orders"), ("a/openapi.yaml", 7, "Invoice")]

    def test_only_a_route_bearing_file_is_read_for_its_prefix(self):
        log: list[str] = []
        controller = "src/main/java/com/acme/OrderController.java"
        spec = "openapi: 3.0.3\npaths:\n  /api/v1/orders:\n    get: {}\n"
        files = {controller: SPRING_CONTROLLER, "api/openapi.yaml": spec}
        chunk = [stand_in(controller, ['    @GetMapping("/orders")']), stand_in(ORDER_SERVICE, [ORDER_LINE])]
        found = contract_entries(chunk, contract_paths=["api/openapi.yaml"], read=dict_reader(files, log))
        assert log == [controller, "api/openapi.yaml"]
        assert summary(found) == [("api/openapi.yaml", 3, "/api/v1/orders")]

    def test_migrations_are_read_only_when_there_are_tables(self):
        paths = ["db/changelog/1.sql", "db/changelog/2.sql", "db/changelog/3.sql"]
        files = {"db/changelog/1.sql": "CREATE TABLE orders (id int);\n", "db/changelog/2.sql": "SELECT 1;\n"}
        log: list[str] = []
        comment_only = [stand_in("db/changelog/3.sql", ["-- a comment"])]
        assert contract_entries(comment_only, contract_paths=paths, read=dict_reader(files, log)) == []
        assert log == []
        with_table = [stand_in("db/changelog/3.sql", ["ALTER TABLE orders ADD region text;"])]
        found = contract_entries(with_table, contract_paths=paths, read=dict_reader(files, log))
        assert log == ["db/changelog/1.sql", "db/changelog/2.sql"]
        assert summary(found) == [("db/changelog/1.sql", 1, "orders")]

    def test_an_earlier_migration_in_the_chunk_is_not_read(self):
        paths = ["db/changelog/1.sql", "db/changelog/2.sql", "db/changelog/3.sql"]
        log: list[str] = []
        chunk = [
            stand_in("db/changelog/2.sql", ["CREATE TABLE orders (id int);"]),
            stand_in("db/changelog/3.sql", ["ALTER TABLE orders ADD region text;"]),
        ]
        contract_entries(chunk, contract_paths=paths, read=dict_reader({}, log))
        assert log == ["db/changelog/1.sql"]

    def test_sql_and_xml_contracts_are_never_read_as_specs(self):
        log: list[str] = []
        paths = ["db/changelog/1.sql", "db/changelog/2.xml", "other/orders.sql"]
        chunk = [stand_in("src/orders.py", ["save_order(order)"])]
        assert contract_entries(chunk, contract_paths=paths, read=dict_reader({}, log)) == []
        assert log == []
