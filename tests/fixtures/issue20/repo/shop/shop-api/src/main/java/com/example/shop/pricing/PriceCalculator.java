package com.example.shop.pricing;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.annotation.Nullable;
import java.math.BigDecimal;
import java.util.Objects;
import org.springframework.transaction.annotation.Transactional;

public class PriceCalculator {

    @Deprecated
    public static final int MAX_ITEMS = 50;

    @Nullable
    @JsonProperty("rate")
    private BigDecimal taxRate;

    private boolean open;

    @Transactional
    public BigDecimal applyDiscount(BigDecimal amount, int items) {
        BigDecimal factor = items > 10 ? new BigDecimal("0.95") : BigDecimal.ONE;
        return amount.multiply(factor);
    }

    public void setOpen(boolean value) {
        this.open = value;
    }

    public BigDecimal total(BigDecimal unit, int items) {
        Objects.requireNonNull(unit, "unit");
        if (!open || items > MAX_ITEMS) {
            throw new IllegalStateException("closed or too many items");
        }
        var gross = applyDiscount(unit.multiply(BigDecimal.valueOf(items)), items);
        return gross.add(gross.multiply(taxRate));
    }
}
