--liquibase formatted sql

--changeset acme:2
CREATE TABLE idempotency_keys (
    tenant_id VARCHAR(36) NOT NULL,
    connector_id VARCHAR(36) NOT NULL,
    key VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
