package com.aspire.pricing;

import com.aspire.coupon.CouponDTO;

public class PricingEngine {

    private final CurrencyConverter converter;

    public PricingEngine(CurrencyConverter converter) {
        this.converter = converter;
    }

    /**
     * Calculates the final order total.
     * Converts from the order currency to USD, applies discount, returns USD total.
     *
     * @param subtotal     amount in the order's currency
     * @param currency     ISO currency code (USD, EUR, GBP, JPY)
     * @param coupon       optional coupon to apply (null = no discount)
     * @return final total in USD, rounded to 2 decimal places
     */
    public double calculateTotal(double subtotal, String currency, CouponDTO coupon) {
        double usdSubtotal = converter.toUSD(subtotal, currency);

        double discount = 0.0;
        if (coupon != null) {
            if ("PERCENTAGE".equals(coupon.getDiscountType())) {
                discount = usdSubtotal * (coupon.getDiscountValue() / 100.0);
            } else if ("FIXED".equals(coupon.getDiscountType())) {
                discount = coupon.getDiscountValue();
            }
        }

        double total = Math.max(0.0, usdSubtotal - discount);
        return Math.round(total * 100) / 100.0;   // round to 2dp
    }
}
