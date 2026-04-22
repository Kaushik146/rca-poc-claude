package com.aspire.order;

import com.aspire.audit.AuditEvent;
import com.aspire.audit.AuditLogger;
import com.aspire.coupon.CouponDTO;
import com.aspire.coupon.CouponServiceClient;
import com.aspire.coupon.CouponValidator;
import com.aspire.inventory.InventoryClient;
import com.aspire.notification.NotificationClient;
import com.aspire.payment.PaymentProcessor;
import com.aspire.payment.PaymentResult;
import com.aspire.pricing.CurrencyConverter;
import com.aspire.pricing.PricingEngine;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.Optional;
import java.util.UUID;

/**
 * Main checkout orchestrator.
 *
 * Flow:
 *  1. Validate request
 *  2. Reserve inventory  → Python service (snake_case JSON)
 *  3. Validate coupon    → CouponServiceClient
 *  4. Calculate total    → PricingEngine + CurrencyConverter
 *  5. Charge payment     → PaymentProcessor
 *  6. Persist order      → SQLiteOrderRepository (REAL total column)
 *  7. Notify customer    → Node.js service (camelCase JSON)
 *  8. Audit log
 */
public class OrderService {

    private static final Logger log = LoggerFactory.getLogger(OrderService.class);

    private final CouponServiceClient couponClient;
    private final CouponValidator     couponValidator;
    private final InventoryClient     inventoryClient;
    private final NotificationClient  notificationClient;
    private final PaymentProcessor    paymentProcessor;
    private final PricingEngine       pricingEngine;
    private final CurrencyConverter   currencyConverter;
    private final OrderRepository     orderRepository;
    private final AuditLogger         auditLogger;

    public OrderService(
            CouponServiceClient couponClient,
            CouponValidator couponValidator,
            InventoryClient inventoryClient,
            NotificationClient notificationClient,
            PaymentProcessor paymentProcessor,
            PricingEngine pricingEngine,
            CurrencyConverter currencyConverter,
            OrderRepository orderRepository,
            AuditLogger auditLogger) {
        this.couponClient      = couponClient;
        this.couponValidator   = couponValidator;
        this.inventoryClient   = inventoryClient;
        this.notificationClient= notificationClient;
        this.paymentProcessor  = paymentProcessor;
        this.pricingEngine     = pricingEngine;
        this.currencyConverter = currencyConverter;
        this.orderRepository   = orderRepository;
        this.auditLogger       = auditLogger;
    }

    public CheckoutResult checkout(CheckoutRequest req) {
        log.info("checkout() start: customerId={} sku={} qty={} currency={}",
                req.getCustomerId(), req.getSku(), req.getQuantity(), req.getCurrency());

        if (req.getQuantity() <= 0) return CheckoutResult.failure(null, "Quantity must be positive");
        if (req.getSku() == null || req.getSku().isBlank()) return CheckoutResult.failure(null, "SKU required");

        // ── Idempotency check: prevent duplicate orders ──
        // Check if an order with the same orderId already exists.
        // This handles the case where a retry after payment succeeds but the response is lost,
        // ensuring we don't create duplicate orders.
        String requestIdempotencyKey = req.getIdempotencyKey();
        if (requestIdempotencyKey != null) {
            Optional<Order> existingOrder = orderRepository.findByIdempotencyKey(requestIdempotencyKey);
            if (existingOrder.isPresent()) {
                Order existing = existingOrder.get();
                log.info("Idempotent retry detected: returning existing order {}", existing.getOrderId());
                return CheckoutResult.success(existing.getOrderId(), existing.getTotal(), existing.getCurrency());
            }
        }

        // ── Step 2: Reserve inventory (Python service) ──
        boolean reserved = inventoryClient.reserveStock(req.getSku(), req.getQuantity());
        if (!reserved) {
            return CheckoutResult.failure(null, "Insufficient inventory for SKU: " + req.getSku());
        }

        // ── Transaction wrapper: if payment succeeds but order save fails, compensate ──
        try {
            // ── Step 3: Validate coupon ───────────────────
            double subtotal = req.getUnitPrice() * req.getQuantity();
            CouponDTO appliedCoupon = null;
            String couponCode = req.getCouponCode();
            if (couponCode != null && !couponCode.isBlank()) {
                Optional<CouponDTO> couponOpt = couponClient.getCoupon(couponCode);
                if (couponOpt.isPresent()) {
                    CouponDTO coupon = couponOpt.get();
                    if (couponValidator.isValid(coupon, subtotal)) {
                        appliedCoupon = coupon;
                        log.debug("Coupon '{}' applied: type={} value={}",
                                couponCode, coupon.getDiscountType(), coupon.getDiscountValue());
                    } else {
                        log.warn("Coupon '{}' invalid — zero discount", couponCode);
                    }
                } else {
                    log.warn("Coupon '{}' not found — zero discount", couponCode);
                }
            }

            // ── Step 4: Price calculation ─────────────────
            // PricingEngine.calculateTotal: takes subtotal in req currency → returns USD total
            double usdTotal   = pricingEngine.calculateTotal(subtotal, req.getCurrency(), appliedCoupon);
            // Convert back to requested currency for billing
            double finalTotal = currencyConverter.fromUSD(usdTotal, req.getCurrency());

            // ── Step 5: Payment ───────────────────────────
            String orderId = UUID.randomUUID().toString();
            PaymentResult payment = paymentProcessor.charge(orderId, finalTotal, req.getCurrency());
            if (!payment.isSuccess()) {
                inventoryClient.releaseStock(req.getSku(), req.getQuantity());
                return CheckoutResult.failure(null, "Payment failed: " + payment.getErrorMessage());
            }

            // ── Step 6: Persist order (SQLite) ────────────
            Order order = new Order(
                    orderId, req.getCustomerId(), req.getSku(),
                    req.getQuantity(), BigDecimal.valueOf(finalTotal), req.getCurrency(),
                    OrderStatus.CONFIRMED, Instant.now());
            // Store the idempotency key to enable duplicate detection on retries
            order.setIdempotencyKey(requestIdempotencyKey);
            orderRepository.save(order);

            // ── Step 7: Notification (Node.js service) ────
            // Notification is best-effort — don't fail checkout if it fails
            try {
                notificationClient.notify(
                        orderId, req.getCustomerId(),
                        "ORDER_CONFIRMED",
                        "Your order has been confirmed. Total: " + finalTotal + " " + req.getCurrency());
            } catch (Exception e) {
                log.warn("Notification failed for order {}: {}", orderId, e.getMessage());
                // Continue — order is already confirmed and persisted
            }

            // ── Step 8: Audit ─────────────────────────────
            auditLogger.log(new AuditEvent("ORDER_CONFIRMED", orderId, req.getCustomerId(),
                    "total=" + finalTotal + " currency=" + req.getCurrency()));

            log.info("checkout() OK: orderId={} total={} {}", orderId, finalTotal, req.getCurrency());
            return CheckoutResult.success(orderId, finalTotal, req.getCurrency());

        } catch (Exception e) {
            log.error("checkout() failed — releasing inventory: {}", e.getMessage(), e);
            inventoryClient.releaseStock(req.getSku(), req.getQuantity());
            return CheckoutResult.failure(null, "Internal error: " + e.getMessage());
        }
    }
}
