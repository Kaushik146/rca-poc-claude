package com.aspire.notification;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.logging.Logger;

public class HttpNotificationClient implements NotificationClient {

    private static final Logger LOG = Logger.getLogger(HttpNotificationClient.class.getName());
    private final String baseUrl;
    private final ObjectMapper mapper = new ObjectMapper();
    private static final int MAX_RETRIES = 3;

    // Static singleton HttpClient for connection reuse across all instances
    private static final HttpClient HTTP_CLIENT = HttpClient.newBuilder()
        .version(HttpClient.Version.HTTP_2)
        .connectTimeout(Duration.ofSeconds(5))
        .build();

    public HttpNotificationClient(String baseUrl) {
        this.baseUrl = baseUrl;
    }

    @Override
    public boolean notify(String orderId, String customerId, String eventType, String message) {
        try {
            ObjectNode body = mapper.createObjectNode();
            // Field names match Node.js server expectation (camelCase)
            body.put("orderId",       orderId);
            body.put("customerEmail", customerId);
            body.put("type",          eventType);
            body.put("message",       message);
            body.put("timestamp",     System.currentTimeMillis());

            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/notify"))
                .header("Content-Type", "application/json")
                .timeout(Duration.ofSeconds(10))
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();

            HttpResponse<String> res = sendWithRetry(req);
            return res.statusCode() == 200 || res.statusCode() == 201;
        } catch (Exception e) {
            throw new RuntimeException("NotificationClient.notify failed", e);
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
