--liquibase formatted sql

--changeset acme:1
CREATE TABLE connectors (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    name VARCHAR(255) NOT NULL
);
