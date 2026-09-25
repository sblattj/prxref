package com.example.inventory

import com.fasterxml.jackson.databind.ObjectMapper
import java.time.LocalDate

/**
 * Stock level reporting for the inventory service.
 *
 * Levels are rendered one per line, lowest stock first.
 */

data class StockLevel(val sku: String, val onHand: Int)

fun formatLevel(level: StockLevel): String {
    return "${level.sku}: ${level.onHand}"
}

class StockReport(private val day: LocalDate) {

    fun title(): String = "Stock on $day"

    fun render(levels: List<StockLevel>): String {
        val lines = levels.sortedBy(StockLevel::onHand).map { formatLevel(it) }
        return ObjectMapper().writeValueAsString(listOf(title()) + lines)
    }
}
