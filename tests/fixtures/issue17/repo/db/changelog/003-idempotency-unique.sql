--liquibase formatted sql

--changeset acme:3
CREATE UNIQUE INDEX ux_idempotency_keys ON idempotency_keys (tenant_id, key);
