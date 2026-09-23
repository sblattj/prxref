# Ingest Config Schema Spec — v3.0 (excerpt)

Normative for `config/ingest.yaml` and `migrations/`.

## C-1 Pinned schema version

`schema_version` MUST be the exact string `"3.0"`.

- `"2.9"` (or anything older) is rejected as unsupported.
- `"3"` and `"3.0.0"` are rejected: the field is an exact-match string, not a
  semver range. The registry compares string equality and nothing else.

## C-2 Migration file naming

Migration files MUST be named `V<n>__<slug>.sql` where:

- `<n>` is the migration's integer version, written without zero padding,
- the separator between `<n>` and `<slug>` is EXACTLY two underscores (`__`),
- `<slug>` uses lowercase words separated by single underscores.

`V24-add-retention-index.sql` (hyphen separator) and `V24_add_retention.sql`
(single underscore separator) are both invalid and rejected by the registry.
