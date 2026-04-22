"""Centralized configuration for RCA pipeline agents."""
import os

# LLM settings
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_MAX_ITERATIONS = int(os.getenv("LLM_MAX_ITERATIONS", "12"))

# Circuit breaker
CB_MAX_FAILURES = int(os.getenv("CB_MAX_FAILURES", "3"))
CB_RESET_TIMEOUT = int(os.getenv("CB_RESET_TIMEOUT", "60"))

# Thresholds
ANOMALY_Z_THRESHOLD = float(os.getenv("ANOMALY_Z_THRESHOLD", "2.5"))
CORRELATION_THRESHOLD = float(os.getenv("CORRELATION_THRESHOLD", "0.8"))
DEFAULT_DAU = int(os.getenv("DEFAULT_DAU", "5000"))
REVENUE_THRESHOLD = float(os.getenv("REVENUE_THRESHOLD", "100"))

# Service directories
SERVICE_DIRS = {
    "java-order-service": "java-order-service/src/main/java",
    "python-inventory-service": "python-inventory-service",
    "node-notification-service": "node-notification-service",
}

# Database paths
DB_PATHS = ["order-db.sqlite", "orders.db", "order.db"]

# Report output
REPORT_DIR = os.getenv("RCA_REPORT_DIR", "./reports")
