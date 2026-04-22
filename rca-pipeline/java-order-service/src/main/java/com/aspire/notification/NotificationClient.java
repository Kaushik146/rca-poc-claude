package com.aspire.notification;
public interface NotificationClient {
    boolean notify(String orderId, String customerId, String eventType, String message);
}
