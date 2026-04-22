package com.aspire.inventory;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.logging.Logger;

/**
 * Calls the Python inventory service (Flask, port 5003).
 *
 * API contract (Python side):
 *   POST /reserve  — body: { "product_id": str, "quantity": int }
 *   POST /release  — body: { "product_id": str, "quantity": int }
 *   GET  /stock/<product_id>
 */
public class HttpInventoryClient implements InventoryClient {

    private static final Logger LOG = Logger.getLogger(HttpInventoryClient.class.getName());
    private final String baseUrl;
    private final ObjectMapper mapper = new ObjectMapper();
    private static final int MAX_RETRIES = 3;

    // Static singleton HttpClient for connection reuse across all instances
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder()
        .version(HttpClient.Version.HTTP_2)
        .connectTimeout(Duration.ofSeconds(5))
        .build();

    public HttpInventoryClient(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    @Override
    public boolean reserveStock(String productId, int quantity) {
        return postAction("/reserve", productId, quantity, "reserved");
    }

    @Override
    public boolean releaseStock(String productId, int quantity) {
        return postAction("/release", productId, quantity, "released");
    }

    @Override
    public int getStock(String productId) {
        try {
            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/stock/" + productId))
                .timeout(Duration.ofSeconds(10))
                .GET().build();
            HttpResponse<String> res = sendWithRetry(req);
            if (res.statusCode() != 200) {
                throw new RuntimeException("Inventory API returned status: " + res.statusCode());
            }
            JsonNode json = mapper.readTree(res.body());
            return json.path("available").asInt(0);
        } catch (Exception e) {
            throw new RuntimeException("InventoryClient.getStock failed", e);
        }
    }

    private boolean postAction(String endpoint, String productId, int quantity, String resultField) {
        try {
            ObjectNode body = mapper.createObjectNode();
            // Field names that match the Python Flask API (snake_case)
            body.put("product_id", productId);
            body.put("quantity",   quantity);

            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + endpoint))
                .header("Content-Type", "application/json")
                .timeout(Duration.ofSeconds(10))
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();

            HttpResponse<String> res = sendWithRetry(req);
            if (res.statusCode() != 200) return false;
            JsonNode json = mapper.readTree(res.body());
            return json.path(resultField).asBoolean(false);
        } catch (Exception e) {
            throw new RuntimeException("InventoryClient" + endpoint + " failed", e);
        }
    }

    private HttpResponse<String> sendWithRetry(HttpRequest request) throws Exception {
        HttpResponse<String> response = null;
        Exception lastException = null;
        for (int attempt = 0; attempt < MAX_RETRIES; attempt++) {
            try {
                response = HTTP_CLIENT.send(request, HttpResponse.BodyHandlers.ofString());
                // Log 4xx client errors for debugging
                if (response.statusCode() >= 400 && response.statusCode() < 500) {
                    LOG.warning("Client error " + response.statusCode() + ": " + response.body());
                }
                if (response.statusCode() < 500) break; // Don't retry client errors
                lastException = new RuntimeException("Server error: " + response.statusCode());
            } catch (Exception e) {
                lastException = e;
            }
            if (attempt < MAX_RETRIES - 1) {
                try {
                    long delay = (long)(Math.pow(2, attempt) * 100 + Math.random() * 100);
                    Thread.sleep(delay);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
        if (response == null || response.statusCode() >= 500) {
            throw new RuntimeException("Service call failed after " + MAX_RETRIES + " retries", lastException);
        }
        return response;
    }
}
