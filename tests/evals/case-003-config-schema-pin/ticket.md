# DATA-177: Pin ingest config to schema 3.0 and ship retention index

**Type:** Task  **Priority:** P1  **Epic:** Retention queries

## Summary

Adopt the ingest config schema pinned by the platform registry and add the
backfill migration for retention queries.

## Description

The config registry started validating `schema_version` strictly as part of
the 3.0 rollout. Our service config must be updated to the pinned value, and
the retention dashboard needs its backing index, delivered as a migration
file the registry will accept.

Naming and version rules are owned by the config schema spec; follow it
exactly — the registry rejects anything else at startup.

## Acceptance Criteria

- [ ] `config/ingest.yaml` passes registry validation on boot
- [ ] New migration file is accepted by the migration registry
- [ ] Retention queries use the new index
- [ ] Migrations apply in order on a fresh database
- [ ] Conformance per docs/config-schema.md (excerpt, normative)
