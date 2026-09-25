package com.example.shop.api;

import com.example.shop.core.Money;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class OrderMapper {

    private static final Logger LOG = LoggerFactory.getLogger("orders");

    private final ObjectMapper mapper = new ObjectMapper();

    public String describe(int count) {
        return "orders: " + count;
    }

    public String toJson(List<Money> totals) throws Exception {
        LOG.debug("mapping {} totals", totals.size());
        return mapper.writeValueAsString(totals);
    }
}
