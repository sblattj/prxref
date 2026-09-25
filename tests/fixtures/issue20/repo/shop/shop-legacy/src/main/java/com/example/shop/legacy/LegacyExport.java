package com.example.shop.legacy;

import com.fasterxml.jackson.databind.ObjectMapper;

public class LegacyExport {

    private final ObjectMapper writer = new ObjectMapper();

    public String export(Object order) throws Exception {
        return writer.writeValueAsString(order);
    }
}
