package com.aspire.inventory;
public interface InventoryClient {
    /** Reserve @quantity units of @productId. Returns true if successful. */
    boolean reserveStock(String productId, int quantity);
    /** Release previously reserved @quantity units back to stock. */
    boolean releaseStock(String productId, int quantity);
    /** Get available stock count for @productId. */
    int     getStock(String productId);
}
