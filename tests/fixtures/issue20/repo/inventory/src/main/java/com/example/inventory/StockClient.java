package com.example.inventory;

import com.example.inventory.model.Sku;
import com.fasterxml.jackson.databind.ObjectMapper;

public class StockClient {

    private final ObjectMapper json = new ObjectMapper();

    public String fetch(Sku sku) throws Exception {
        return json.writeValueAsString(sku);
    }

    public StockLevel parse(String body) throws Exception {
        return json.readValue(body, StockLevel.class);
    }
}
