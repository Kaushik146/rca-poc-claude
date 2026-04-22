package com.aspire.pricing;

import java.util.Map;

/**
 * Currency conversion utility.
 *
 * Rates are "1 unit of currency = X USD".
 * So EUR 1.00 = USD 1.08.
 */
public class CurrencyConverter {

    // How many USD per 1 unit of the given currency
    private static final Map<String, Double> USD_RATES = Map.of(
        "USD", 1.0,
        "EUR", 1.08,    // 1 EUR = $1.08 USD
        "GBP", 1.27,    // 1 GBP = $1.27 USD
        "JPY", 0.0067   // 1 JPY = $0.0067 USD
    );

    /**
     * Convert @amount in @fromCurrency to USD.
     * e.g. toUSD(10.0, "EUR") → 10.0 * 1.08 = 10.80
     */
    public double toUSD(double amount, String fromCurrency) {
        if (fromCurrency == null || fromCurrency.isBlank())
            throw new IllegalArgumentException("Currency code required");
        Double rate = USD_RATES.get(fromCurrency.toUpperCase());
        if (rate == null)
            throw new IllegalArgumentException("Unknown currency: " + fromCurrency);
        return amount * rate;
    }

    /**
     * Convert @usdAmount from USD to @toCurrency.
     * e.g. fromUSD(10.80, "EUR") → 10.80 / 1.08 = 10.00
     */
    public double fromUSD(double usdAmount, String toCurrency) {
        if (toCurrency == null || toCurrency.isBlank())
            throw new IllegalArgumentException("Currency code required");
        if ("USD".equalsIgnoreCase(toCurrency)) return usdAmount;
        Double rate = USD_RATES.get(toCurrency.toUpperCase());
        if (rate == null)
            throw new IllegalArgumentException("Unknown currency: " + toCurrency);
        return usdAmount / rate;
    }
}
