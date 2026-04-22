package com.aspire.payment;
import java.util.UUID;
/** Used in tests — always succeeds unless amount < 0. */
public class MockPaymentProcessor implements PaymentProcessor {
    @Override public PaymentResult charge(String orderId, double amount, String currency) {
        if (amount < 0) return PaymentResult.fail("Negative amount");
        return PaymentResult.ok("PAY-" + UUID.randomUUID().toString().substring(0, 8).toUpperCase());
    }
    @Override public PaymentResult refund(String orderId, double amount) {
        if (amount < 0) return PaymentResult.fail("Negative refund");
        return PaymentResult.ok("REF-" + UUID.randomUUID().toString().substring(0, 8).toUpperCase());
    }
}
