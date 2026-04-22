package com.aspire.coupon;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Optional;
import java.util.logging.Logger;

public class HttpCouponServiceClient implements CouponServiceClient {

    private static final Logger LOG = Logger.getLogger(HttpCouponServiceClient.class.getName());
    private final String     baseUrl;
    private final HttpClient http;
    private final ObjectMapper mapper = new ObjectMapper();
    private static final int MAX_RETRIES = 3;

    public HttpCouponServiceClient(String baseUrl) {
        this.baseUrl = baseUrl;
        this.http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(3)).build();
    }

    @Override
    public Optional<CouponDTO> getCoupon(String code) {
        try {
            HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(baseUrl + "/coupons/" + code))
                .timeout(Duration.ofSeconds(10))
                .GET().build();
            HttpResponse<String> res = sendWithRetry(req);
            if (res.statusCode() == 404) return Optional.empty();
            if (res.statusCode() != 200) throw new RuntimeException("Coupon API error: " + res.statusCode());
            return Optional.of(mapper.readValue(res.body(), CouponDTO.class));
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new RuntimeException("CouponServiceClient failed", e);
        }
    }

    private HttpResponse<String> sendWithRetry(HttpRequest request) throws Exception {
        HttpResponse<String> response = null;
        Exception lastException = null;
        for (int attempt = 0; attempt < MAX_RETRIES; attempt++) {
            try {
                response = http.send(request, HttpResponse.BodyHandlers.ofString());
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
