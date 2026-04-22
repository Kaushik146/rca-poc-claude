package com.aspire.payment;
public interface PaymentProcessor {
    PaymentResult charge(String orderId, double amount, String currency);
    PaymentResult refund(String orderId, double amount);
}
