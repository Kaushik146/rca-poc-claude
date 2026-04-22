package com.aspire.order;
import java.util.Optional;
public interface OrderRepository {
    void         save(Order order);
    Optional<Order> findById(String id);
    Optional<Order> findByIdempotencyKey(String idempotencyKey);
    void         updateStatus(String id, OrderStatus status);
}
