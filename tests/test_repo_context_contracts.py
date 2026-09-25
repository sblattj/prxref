"""Unit tests for :mod:`prxref.repo_contracts`, the contract excerpters.

The module is pure, so every fixture is an inline string: OpenAPI in YAML and
JSON, a JSON Schema, a Liquibase formatted-SQL migration, and Liquibase XML,
YAML and JSON changelogs. The running example is the issue's: a transport
endpoint whose idempotency key is unique on (tenant, connector, key), where the
table is ``idempotency_keys`` and the schema is ``IdempotencyKey``.
"""
from __future__ import annotations

import json
import re

import pytest

from prxref.repo_contracts import (
    MAX_CONTRACT_CHARS,
    MAX_CONTRACT_LINES,
    Excerpt,
    json_schema_excerpts,
    liquibase_excerpts,
    normalize_name,
    openapi_json_excerpts,
    openapi_yaml_excerpts,
    route_key,
    sql_excerpts,
)

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
MARKER = re.compile(ELLIPSIS + r" (\d+) more lines")


def line_of(text: str, prefix: str, after: int = 0) -> int:
    """1-based number of the first line after line ``after`` that starts with ``prefix``."""
    return next(
        number
        for number, line in enumerate(text.split("\n"), 1)
        if number > after and line.startswith(prefix)
    )


OPENAPI_YAML = """\
openapi: 3.0.3
info:
  title: Acme connectors
  version: 1.0.0
paths:
  /connectors/{connectorId}/transports:
    parameters:
      - name: connectorId
        in: path
        required: true
        schema:
          type: string

    post:
      operationId: createTransport
      summary: Create a transport for a connector
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/IdempotencyKey'
      responses:
        '201':
          description: Created
    get:
      operationId: "listTransports"
      responses:
        '200':
          description: OK

  "/tenants/{tenantId}":
    get:
      operationId: getTenant
      description: |
        An example kept as text, not as an operation:
        post:
          operationId: createTransport
      responses:
        '200':
          description: OK
          links:
            CreateOne:
              operationId: createTransport
components:
  schemas:
    IdempotencyKey:
      type: object
      description: Unique on (tenant, connector, key).
      required: [tenant_id, connector_id, key]
      properties:
        key:
          type: string

    Tenant:
      type: object
"""

TRANSPORTS = "/connectors/{connectorId}/transports"


class TestNormalizeName:
    def test_plural_snake_case_and_pascal_case_fold_to_one_key(self):
        assert normalize_name("idempotency_keys") == "idempotencykey"
        assert normalize_name("IdempotencyKey") == "idempotencykey"
        assert normalize_name("idempotency-keys") == "idempotencykey"

    def test_exactly_one_trailing_s_is_stripped(self):
        assert normalize_name("Keys") == "key"
        assert normalize_name("statuss") == "status"
        assert normalize_name("s") == ""


class TestRouteKey:
    @pytest.mark.parametrize("route", [
        "/connectors/{connectorId}/transports",
        "/connectors/{id}/transports",
        "/connectors/:id/transports",
        "/connectors/<id>/transports",
        "/connectors/<int:id>/transports",
        "/connectors/{id:[0-9]+}/transports",
        "/connectors/{id}/transports/",
        "//connectors//{id}/transports",
        "connectors/{id}/transports",
    ])
    def test_every_parameter_style_gives_one_key(self, route):
        assert route_key(route) == "/connectors/{}/transports"

    def test_case_is_kept(self):
        assert route_key("/Connectors/{id}") != route_key("/connectors/{id}")

    def test_a_parameter_inside_a_segment_is_replaced_in_place(self):
        assert route_key("/files/{name}.json") == "/files/{}.json"

    def test_root_and_blank(self):
        assert route_key("/") == "/"
        assert route_key("") == ""
        assert route_key("   ") == ""


class TestOpenApiYaml:
    @pytest.mark.parametrize("route", [
        "/connectors/{id}/transports",
        "/connectors/:id/transports",
        "/connectors/<int:id>/transports/",
    ])
    def test_route_finds_the_path_item(self, route):
        [excerpt] = openapi_yaml_excerpts(OPENAPI_YAML, routes=[route])
        assert excerpt.symbol == TRANSPORTS
        assert excerpt.line == line_of(OPENAPI_YAML, f"  {TRANSPORTS}:")
        lines = excerpt.text.split("\n")
        assert lines[0] == f"{TRANSPORTS}:"
        assert lines[1] == "  parameters:"
        assert lines[-1] == "        description: OK"
        assert "\n\n  post:" in excerpt.text
        assert "operationId: createTransport" in excerpt.text
        assert "listTransports" in excerpt.text
        assert "tenants" not in excerpt.text

    def test_operation_id_finds_the_enclosing_operation_only(self):
        [excerpt] = openapi_yaml_excerpts(OPENAPI_YAML, operation_ids=["createTransport"])
        assert excerpt == Excerpt(
            line=line_of(OPENAPI_YAML, "    post:"),
            symbol="createTransport",
            text=excerpt.text,
        )
        lines = excerpt.text.split("\n")
        assert lines[:2] == ["post:", "  operationId: createTransport"]
        assert lines[-1] == "      description: Created"
        assert "listTransports" not in excerpt.text

    def test_quoted_operation_id_and_quoted_path_key(self):
        [operation] = openapi_yaml_excerpts(OPENAPI_YAML, operation_ids=["listTransports"])
        assert operation.symbol == "listTransports"
        assert operation.text.startswith("get:\n")
        [path_item] = openapi_yaml_excerpts(OPENAPI_YAML, routes=["/tenants/:tenantId"])
        assert path_item.symbol == "/tenants/{tenantId}"
        assert path_item.line == line_of(OPENAPI_YAML, '  "/tenants/{tenantId}":')
        assert path_item.text.split("\n")[-1] == "            operationId: createTransport"

    @pytest.mark.parametrize("name", ["idempotency_keys", "IdempotencyKey", "idempotency-key"])
    def test_schema_matches_by_normalized_name(self, name):
        [excerpt] = openapi_yaml_excerpts(OPENAPI_YAML, schemas=[name])
        assert excerpt.symbol == "IdempotencyKey"
        assert excerpt.line == line_of(OPENAPI_YAML, "    IdempotencyKey:")
        assert excerpt.text.split("\n") == [
            "IdempotencyKey:",
            "  type: object",
            "  description: Unique on (tenant, connector, key).",
            "  required: [tenant_id, connector_id, key]",
            "  properties:",
            "    key:",
            "      type: string",
        ]

    def test_a_route_and_its_operation_id_give_one_excerpt(self):
        excerpts = openapi_yaml_excerpts(
            OPENAPI_YAML, routes=["/connectors/:id/transports"], operation_ids=["createTransport"],
        )
        assert [e.symbol for e in excerpts] == [TRANSPORTS]

    def test_results_come_in_line_order(self):
        excerpts = openapi_yaml_excerpts(
            OPENAPI_YAML,
            schemas=["idempotency_keys"],
            operation_ids=["getTenant"],
            routes=["/connectors/:id/transports"],
        )
        assert [e.symbol for e in excerpts] == [TRANSPORTS, "getTenant", "IdempotencyKey"]
        assert [e.line for e in excerpts] == sorted(e.line for e in excerpts)

    def test_nothing_asked_or_nothing_matching_gives_nothing(self):
        assert openapi_yaml_excerpts(OPENAPI_YAML) == []
        assert openapi_yaml_excerpts(OPENAPI_YAML, routes=["/connectors"], schemas=["widgets"]) == []
        assert openapi_yaml_excerpts("", routes=["/connectors"]) == []

    def test_swagger_two_definitions_hold_schemas(self):
        spec = "swagger: '2.0'\ndefinitions:\n  IdempotencyKey:\n    type: object\n  Tenant:\n    type: object\n"
        [excerpt] = openapi_yaml_excerpts(spec, schemas=["idempotency_keys"])
        assert excerpt == Excerpt(3, "IdempotencyKey", "IdempotencyKey:\n  type: object")

    def test_a_path_key_may_hold_a_colon_that_is_not_followed_by_a_space(self):
        spec = "paths:\n  /connectors/{id}:cancel:\n    post:\n      operationId: cancelConnector\n"
        [excerpt] = openapi_yaml_excerpts(spec, routes=["/connectors/{connectorId}:cancel"])
        assert (excerpt.line, excerpt.symbol) == (2, "/connectors/{id}:cancel")
        assert excerpt.text.split("\n")[-1] == "    operationId: cancelConnector"

    def test_crlf_line_endings_and_a_byte_order_mark(self):
        text = chr(0xFEFF) + OPENAPI_YAML.replace("\n", "\r\n")
        assert openapi_yaml_excerpts(text, routes=["/connectors/:id/transports"], schemas=["IdempotencyKey"]) == (
            openapi_yaml_excerpts(OPENAPI_YAML, routes=["/connectors/:id/transports"], schemas=["IdempotencyKey"])
        )

    def test_a_json_text_goes_through_the_json_excerpter(self):
        text = json.dumps(OPENAPI_JSON, indent=2)
        assert openapi_yaml_excerpts(text, routes=["/connectors/:id/transports"]) == openapi_json_excerpts(
            text, routes=["/connectors/:id/transports"],
        )


def _schema_doc(body: list[str]) -> str:
    return "\n".join([
        "components:",
        "  schemas:",
        "    Wide:",
        *(f"      {line}" for line in body),
        "    Other:",
        "      type: object",
    ])


def _operations(count: int, filler: int) -> str:
    lines = ["paths:", f"  {TRANSPORTS}:"]
    for method, op_id in [("get", "listTransports"), ("put", "replaceTransport"), ("post", "createTransport")][:count]:
        lines += [f"    {method}:", f"      operationId: {op_id}"]
        lines += [f"      x-note-{i:02d}: filler" for i in range(filler)]
    return "\n".join(lines)


class TestCaps:
    def test_a_sixty_line_schema_is_cut_to_forty_lines(self):
        body = ["type: object", "properties:", *(f"  field_{i:02d}: {{type: string}}" for i in range(57))]
        [excerpt] = openapi_yaml_excerpts(_schema_doc(body), schemas=["wide"])
        source = ["Wide:", *(f"  {line}" for line in body)]
        assert len(source) == 60
        lines = excerpt.text.split("\n")
        assert len(lines) == MAX_CONTRACT_LINES == 40
        assert lines[:39] == source[:39]
        assert lines[-1] == f"{ELLIPSIS} 21 more lines"
        assert len(excerpt.text) <= MAX_CONTRACT_CHARS

    def test_forty_lines_stay_whole_and_forty_one_are_cut(self):
        whole = openapi_yaml_excerpts(_schema_doc([f"f{i}: x" for i in range(39)]), schemas=["wide"])[0]
        assert len(whole.text.split("\n")) == 40
        assert not MARKER.search(whole.text)
        cut = openapi_yaml_excerpts(_schema_doc([f"f{i}: x" for i in range(40)]), schemas=["wide"])[0]
        lines = cut.text.split("\n")
        assert len(lines) == 40
        assert lines[-1] == f"{ELLIPSIS} 2 more lines"

    def test_a_three_thousand_char_block_is_cut_under_the_char_cap(self):
        body = [f"f{i}: {'x' * 300}" for i in range(10)]
        source = ["Wide:", *(f"  {line}" for line in body)]
        assert len("\n".join(source)) > 3000
        [excerpt] = openapi_yaml_excerpts(_schema_doc(body), schemas=["wide"])
        assert len(excerpt.text) <= MAX_CONTRACT_CHARS
        lines = excerpt.text.split("\n")
        kept = len(lines) - 1
        assert lines[:kept] == source[:kept]
        assert lines[-1] == f"{ELLIPSIS} {len(source) - kept} more lines"
        one_more = "\n".join([*source[: kept + 1], f"{ELLIPSIS} {len(source) - kept - 1} more lines"])
        assert len(one_more) > MAX_CONTRACT_CHARS

    def test_a_single_overlong_line_is_itself_cut(self):
        columns = ", ".join(f"column_{i:04d} BIGINT" for i in range(150))
        text = f"CREATE TABLE idempotency_keys ({columns});"
        assert len(text) > 2500
        [excerpt] = sql_excerpts(text, tables=["IdempotencyKey"])
        assert len(excerpt.text) <= MAX_CONTRACT_CHARS
        assert excerpt.text.startswith("CREATE TABLE idempotency_keys (")
        assert excerpt.text.endswith(ELLIPSIS)

    def test_a_cut_path_item_does_not_absorb_an_operation_it_cuts_off(self):
        spec = _operations(3, 18)
        route = ["/connectors/:id/transports"]
        shown = openapi_yaml_excerpts(spec, routes=route, operation_ids=["listTransports"])
        assert [e.symbol for e in shown] == [TRANSPORTS]
        hidden = openapi_yaml_excerpts(spec, routes=route, operation_ids=["createTransport"])
        assert [e.symbol for e in hidden] == [TRANSPORTS, "createTransport"]
        assert "createTransport" not in hidden[0].text
        assert hidden[1].line == line_of(spec, "    post:")


OPENAPI_JSON = {
    "openapi": "3.0.3",
    "info": {"title": "Acme connectors", "version": "1.0.0"},
    "paths": {
        "/tenants/{tenantId}": {
            "post": {"operationId": "updateTenant", "responses": {"200": {"description": "OK"}}},
        },
        TRANSPORTS: {
            "post": {
                "operationId": "createTransport",
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/IdempotencyKey"}}},
                },
                "responses": {"201": {"description": "Created"}},
            },
        },
    },
    "components": {
        "schemas": {
            "Tenant": {"type": "object"},
            "IdempotencyKey": {
                "type": "object",
                "description": "Unique on (tenant, connector, key).",
                "properties": {"key": {"type": "string"}},
            },
        },
    },
}


class TestOpenApiJson:
    TEXT = json.dumps(OPENAPI_JSON, indent=2)

    @pytest.mark.parametrize("route", ["/connectors/{id}/transports", "/connectors/:id/transports"])
    def test_route_finds_the_path_item(self, route):
        [excerpt] = openapi_json_excerpts(self.TEXT, routes=[route])
        assert excerpt.symbol == TRANSPORTS
        assert excerpt.line == line_of(self.TEXT, f'    "{TRANSPORTS}": {{')
        assert excerpt.text == f'"{TRANSPORTS}": ' + json.dumps(OPENAPI_JSON["paths"][TRANSPORTS], indent=2)

    def test_operation_id_finds_its_operation_and_its_own_line(self):
        [excerpt] = openapi_json_excerpts(self.TEXT, operation_ids=["createTransport"])
        assert excerpt.symbol == "createTransport"
        path_line = line_of(self.TEXT, f'    "{TRANSPORTS}": {{')
        assert excerpt.line == line_of(self.TEXT, '      "post": {', after=path_line)
        assert excerpt.text.startswith('"post": {\n  "operationId": "createTransport",')

    def test_schema_matches_by_normalized_name(self):
        [excerpt] = openapi_json_excerpts(self.TEXT, schemas=["idempotency_keys"])
        assert excerpt.symbol == "IdempotencyKey"
        assert excerpt.line == line_of(self.TEXT, '      "IdempotencyKey": {')
        assert "Unique on (tenant, connector, key)." in excerpt.text

    def test_a_route_and_its_operation_id_give_one_excerpt(self):
        excerpts = openapi_json_excerpts(
            self.TEXT, routes=["/connectors/:id/transports"], operation_ids=["createTransport"],
        )
        assert [e.symbol for e in excerpts] == [TRANSPORTS]

    def test_swagger_two_definitions_hold_schemas(self):
        text = json.dumps({"swagger": "2.0", "definitions": {"IdempotencyKey": {"type": "object"}}}, indent=2)
        [excerpt] = openapi_json_excerpts(text, schemas=["idempotency_keys"])
        assert excerpt.line == line_of(text, '    "IdempotencyKey": {') == 4

    @pytest.mark.parametrize("text", ["{not json", "[]", "", '"paths"'])
    def test_invalid_or_non_object_json_gives_nothing(self, text):
        assert openapi_json_excerpts(text, routes=["/connectors/:id/transports"], schemas=["IdempotencyKey"]) == []


JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Transport Request",
    "type": "object",
    "properties": {"idempotencyKey": {"$ref": "#/$defs/IdempotencyKey"}},
    "$defs": {
        "Tenant": {"type": "object"},
        "IdempotencyKey": {
            "type": "object",
            "description": "Unique on (tenant, connector, key).",
            "required": ["tenant_id", "connector_id", "key"],
        },
    },
}


class TestJsonSchema:
    TEXT = json.dumps(JSON_SCHEMA, indent=2)

    def test_a_defs_member_matches_by_normalized_name(self):
        [excerpt] = json_schema_excerpts(self.TEXT, names=["idempotency_keys"])
        assert excerpt.symbol == "IdempotencyKey"
        assert excerpt.line == line_of(self.TEXT, '    "IdempotencyKey": {')
        assert excerpt.text == '"IdempotencyKey": ' + json.dumps(JSON_SCHEMA["$defs"]["IdempotencyKey"], indent=2)

    def test_the_root_title_matches_and_absorbs_its_definitions(self):
        [excerpt] = json_schema_excerpts(self.TEXT, names=["transport_requests", "idempotency_keys"])
        assert excerpt == Excerpt(1, "Transport Request", json.dumps(JSON_SCHEMA, indent=2))

    def test_draft_seven_definitions_and_not_a_property_called_definitions(self):
        schema = {
            "definitions": {"IdempotencyKey": {"type": "object"}},
            "properties": {"definitions": {"type": "object", "properties": {"tenant": {"type": "string"}}}},
        }
        text = json.dumps(schema, indent=2)
        assert [e.symbol for e in json_schema_excerpts(text, names=["IdempotencyKeys"])] == ["IdempotencyKey"]
        assert json_schema_excerpts(text, names=["type"]) == []

    @pytest.mark.parametrize("text", ["{", "[1, 2]", ""])
    def test_invalid_or_non_object_json_gives_nothing(self, text):
        assert json_schema_excerpts(text, names=["IdempotencyKey"]) == []


MIGRATION = """\
--liquibase formatted sql

--changeset acme:1
CREATE TABLE idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL, -- the owning tenant; never null
    connector_id BIGINT NOT NULL,
    key VARCHAR(255) NOT NULL
);
--rollback DROP TABLE idempotency_keys;

--changeset acme:2
ALTER TABLE public."idempotency_keys" ADD CONSTRAINT uq_idempotency_keys_scope UNIQUE (tenant_id, connector_id, key);
--rollback ALTER TABLE idempotency_keys DROP CONSTRAINT uq_idempotency_keys_scope;

--changeset acme:3
create unique index ux on idempotency_keys (tenant_id, key);

--changeset acme:4
CREATE TABLE connectors (
    id BIGSERIAL PRIMARY KEY,
    note TEXT DEFAULT 'a;b'
);
"""


class TestSql:
    def test_create_alter_and_index_statements_match_the_schema_name(self):
        excerpts = sql_excerpts(MIGRATION, tables=["IdempotencyKey"])
        assert [e.line for e in excerpts] == [
            line_of(MIGRATION, "CREATE TABLE idempotency_keys"),
            line_of(MIGRATION, "ALTER TABLE"),
            line_of(MIGRATION, "create unique index"),
        ]
        assert [e.symbol for e in excerpts] == ["idempotency_keys"] * 3
        create, alter, index = (e.text for e in excerpts)
        assert create.split("\n") == [
            "CREATE TABLE idempotency_keys (",
            "    id BIGSERIAL PRIMARY KEY,",
            "    tenant_id BIGINT NOT NULL, -- the owning tenant; never null",
            "    connector_id BIGINT NOT NULL,",
            "    key VARCHAR(255) NOT NULL",
            ");",
        ]
        assert alter == (
            'ALTER TABLE public."idempotency_keys" ADD CONSTRAINT uq_idempotency_keys_scope '
            "UNIQUE (tenant_id, connector_id, key);"
        )
        assert index == "create unique index ux on idempotency_keys (tenant_id, key);"

    def test_a_semicolon_in_a_string_does_not_end_the_statement(self):
        [excerpt] = sql_excerpts(MIGRATION, tables=["connectors"])
        assert excerpt.text.split("\n")[-2:] == ["    note TEXT DEFAULT 'a;b'", ");"]

    def test_an_unrelated_table_is_not_found(self):
        assert sql_excerpts(MIGRATION, tables=["tenants"]) == []
        assert sql_excerpts(MIGRATION) == []

    def test_a_changeset_line_ends_a_statement_that_has_no_semicolon(self):
        text = (
            "--liquibase formatted sql\n"
            "--changeset acme:5\n"
            "CREATE INDEX ix_keys_tenant ON idempotency_keys (tenant_id)\n"
            "--rollback DROP INDEX ix_keys_tenant;\n"
            "--changeset acme:6\n"
            "CREATE TABLE tenants (id BIGINT)\n"
        )
        assert sql_excerpts(text, tables=["idempotency_keys"]) == [
            Excerpt(3, "idempotency_keys", "CREATE INDEX ix_keys_tenant ON idempotency_keys (tenant_id)"),
        ]
        assert sql_excerpts(text, tables=["tenant"]) == [Excerpt(6, "tenants", "CREATE TABLE tenants (id BIGINT)")]

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_quoted_and_schema_qualified_names_use_the_bare_table(self, newline):
        text = newline.join([
            "ALTER TABLE `acme`.`idempotency_keys` ADD UNIQUE KEY uq (tenant_id, `key`);",
            "GO",
            "CREATE NONCLUSTERED INDEX ix ON [dbo].[IdempotencyKeys] ([key])",
            "GO",
            "CREATE TABLE IF NOT EXISTS acme.tenants (id BIGINT);",
        ])
        excerpts = sql_excerpts(text, tables=["idempotency_key"])
        assert [(e.line, e.symbol) for e in excerpts] == [(1, "idempotency_keys"), (3, "IdempotencyKeys")]
        assert excerpts[1].text == "CREATE NONCLUSTERED INDEX ix ON [dbo].[IdempotencyKeys] ([key])"

    def test_block_comments_and_dollar_bodies_hide_their_semicolons(self):
        text = (
            "/* ALTER TABLE idempotency_keys DROP COLUMN key; */\n"
            "CREATE FUNCTION touch() RETURNS trigger AS $body$\n"
            "BEGIN\n"
            "  ALTER TABLE idempotency_keys ADD COLUMN touched BIGINT;\n"
            "END;\n"
            "$body$ LANGUAGE plpgsql;\n"
            "CREATE INDEX ON ONLY idempotency_keys (key);\n"
        )
        assert sql_excerpts(text, tables=["IdempotencyKey"]) == [
            Excerpt(7, "idempotency_keys", "CREATE INDEX ON ONLY idempotency_keys (key);"),
        ]

    def test_crlf_line_endings_and_a_byte_order_mark(self):
        text = chr(0xFEFF) + MIGRATION.replace("\n", "\r\n")
        excerpts = sql_excerpts(text, tables=["IdempotencyKey"])
        assert [e.line for e in excerpts] == [e.line for e in sql_excerpts(MIGRATION, tables=["IdempotencyKey"])]
        assert excerpts[0].text.split("\n")[-1] == ");"
        assert "\r" not in "".join(e.text for e in excerpts)


LIQUIBASE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog>
    <changeSet id="empty" author="acme"/>
    <changeSet id="1" author="acme">
        <createTable tableName="idempotency_keys">
            <column name="id" type="BIGINT"/>
        </createTable>
    </changeSet>
    <!--
    <changeSet id="0" author="acme">
        <dropTable tableName="idempotency_keys"/>
    </changeSet>
    -->
    <changeSet id="2" author="acme">
        <addUniqueConstraint tableName="idempotency_keys" columnNames="tenant_id, connector_id, key"/>
    </changeSet>
    <changeSet id="3" author="acme">
        <createTable tableName="connectors"/>
    </changeSet>
    <changeSet id="4" author="acme">
        <addForeignKeyConstraint baseTableName="transports" referencedTableName="idempotency_keys"
            baseColumnNames="key_id" referencedColumnNames="id" constraintName="fk_transport_key"/>
    </changeSet>
</databaseChangeLog>
"""

LIQUIBASE_YAML = """\
databaseChangeLog:
  - changeSet:
      id: 1
      author: acme
      changes:
        - createTable:
            tableName: idempotency_keys
            columns:
              - column:
                  name: id
                  type: BIGINT
  - changeSet:
      id: 2
      author: acme
      changes:
        - addUniqueConstraint:
            tableName: "idempotency_keys"
            columnNames: tenant_id, connector_id, key
  - changeSet:
      id: 3
      author: acme
      changes:
        - createTable:
            tableName: connectors
"""


class TestLiquibase:
    def test_xml_changesets_holding_the_table(self):
        excerpts = liquibase_excerpts(LIQUIBASE_XML, tables=["IdempotencyKey"])
        assert [(e.line, e.symbol) for e in excerpts] == [
            (line_of(LIQUIBASE_XML, '    <changeSet id="1"'), "idempotency_keys"),
            (line_of(LIQUIBASE_XML, '    <changeSet id="2"'), "idempotency_keys"),
            (line_of(LIQUIBASE_XML, '    <changeSet id="4"'), "idempotency_keys"),
        ]
        assert excerpts[0].text.split("\n") == [
            '<changeSet id="1" author="acme">',
            '    <createTable tableName="idempotency_keys">',
            '        <column name="id" type="BIGINT"/>',
            "    </createTable>",
            "</changeSet>",
        ]
        assert all('id="0"' not in e.text for e in excerpts)

    def test_xml_changeset_for_another_table(self):
        [excerpt] = liquibase_excerpts(LIQUIBASE_XML, tables=["connectors"])
        assert excerpt.text.split("\n") == [
            '<changeSet id="3" author="acme">',
            '    <createTable tableName="connectors"/>',
            "</changeSet>",
        ]

    def test_yaml_changesets_holding_the_table(self):
        excerpts = liquibase_excerpts(LIQUIBASE_YAML, tables=["IdempotencyKey"])
        assert [(e.line, e.symbol) for e in excerpts] == [(2, "idempotency_keys"), (12, "idempotency_keys")]
        first = excerpts[0].text.split("\n")
        assert first[:3] == ["- changeSet:", "    id: 1", "    author: acme"]
        assert first[-1] == "                type: BIGINT"
        assert excerpts[1].text.split("\n")[-1] == "          columnNames: tenant_id, connector_id, key"

    def test_json_changesets_holding_the_table(self):
        changelog = {"databaseChangeLog": [
            {"changeSet": {"id": "1", "author": "acme", "changes": [{"createTable": {"tableName": "connectors"}}]}},
            {"changeSet": {"id": "2", "author": "acme", "changes": [
                {"createIndex": {"tableName": "idempotency_keys", "indexName": "ux", "unique": True}},
            ]}},
        ]}
        text = json.dumps(changelog, indent=2)
        [excerpt] = liquibase_excerpts(text, tables=["IdempotencyKey"])
        second = line_of(text, '      "changeSet": {', after=line_of(text, '      "changeSet": {'))
        assert (excerpt.line, excerpt.symbol) == (second, "idempotency_keys")
        assert excerpt.text.startswith('"changeSet": {\n  "id": "2",')

    def test_an_unrelated_table_or_invalid_json_gives_nothing(self):
        assert liquibase_excerpts(LIQUIBASE_XML, tables=["tenants"]) == []
        assert liquibase_excerpts(LIQUIBASE_YAML, tables=["tenants"]) == []
        assert liquibase_excerpts("{broken", tables=["IdempotencyKey"]) == []
