package com.acme.connectors;

/**
 * Transport configuration for a connector. Exactly one of {@code url} or
 * {@code legacyUrl} must be set; the API contract at
 * api/openapi/connectors.yaml#/components/schemas/TransportConfig documents
 * the two fields as mutually exclusive.
 */
public record TransportConfig(String url, String legacyUrl) {

    public TransportConfig {
        boolean hasUrl = url != null;
        boolean hasLegacyUrl = legacyUrl != null;
        if (hasUrl == hasLegacyUrl) {
            throw new IllegalArgumentException(
                "exactly one of url or legacyUrl must be set");
        }
    }

    public String effectiveUrl() {
        return url != null ? url : legacyUrl;
    }
}
