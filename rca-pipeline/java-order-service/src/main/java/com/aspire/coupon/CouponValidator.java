package com.aspire.coupon;

/**
 * Validates a CouponDTO is applicable to a given order subtotal.
 * discountType must be "PERCENTAGE" or "FIXED" (exact strings from the API).
 */
public class CouponValidator {

    public boolean isValid(CouponDTO coupon, double orderSubtotal) {
        if (coupon == null)       return false;
        if (!coupon.isActive())   return false;
        if (orderSubtotal < coupon.getMinOrderValue()) return false;
        if (coupon.getDiscountValue() <= 0)            return false;

        String type = coupon.getDiscountType();
        if ("PERCENTAGE".equals(type)) {
            return coupon.getDiscountValue() <= 100.0;
        }
        if ("FIXED".equals(type)) {
            return coupon.getDiscountValue() < orderSubtotal;
        }
        return false;  // unknown type
    }
}
