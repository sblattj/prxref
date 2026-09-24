package com.acme.connectors;

import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

@RestController
public class ConnectorService {

    private final Map<String, TransportConfig> transportsByKey = new ConcurrentHashMap<>();

    @PostMapping("/connectors/{connectorId}/transports")
    public TransportConfig createTransport(
            @RequestHeader("X-Tenant-Id") String tenantId,
            @PathVariable String connectorId,
            @RequestBody CreateTransportRequest request) {
        String idempotencyKey = tenantId + ":" + request.idempotencyKey();
        TransportConfig config = new TransportConfig(request.url(), request.legacyUrl());
        transportsByKey.put(idempotencyKey, config);
        return config;
    }
}
