package com.example.reports;

import com.fasterxml.jackson.databind.ObjectMapper;

public class ReportWriter {

    private final ObjectMapper output = new ObjectMapper();

    public String write(Object report) throws Exception {
        return output.writeValueAsString(report);
    }
}
