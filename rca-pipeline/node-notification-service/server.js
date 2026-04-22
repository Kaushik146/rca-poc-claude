/**
 * Node.js Notification Service — built-in http, port 5004
 *
 * API:
 *   POST /notify   { "orderId": str, "customerEmail": str, "type": str, "message": str }
 *   GET  /health   (returns uptime, memory usage, notification count)
 *   GET  /notifications?limit=50&offset=0  (list stored notifications with pagination)
 *
 * Fixes applied:
 *   1. Request body size limit (1MB max)
 *   2. Input validation (orderId, customerEmail, message, type)
 *   3. Security headers (X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, HSTS)
 *   4. Rate limiting (30 requests/minute per IP)
 *   5. Generic error messages (log real errors server-side)
 *   6. Content-Type enforcement (application/json required)
 *   7. Pagination on GET /notifications
 *   8. Graceful shutdown (SIGTERM/SIGINT)
 *   9. Request ID tracking (timestamp + random)
 *   10. Health check endpoint with uptime, memory, count
 */

const http = require("http");

// ─── Constants ───
const MAX_BODY_SIZE = 1024 * 1024; // 1MB
const RATE_LIMIT_WINDOW = 60000; // 1 minute in ms
const RATE_LIMIT_MAX = 30; // max requests per minute per IP
const MAX_NOTIFICATIONS = 10000;
const ALLOWED_NOTIFICATION_TYPES = ["order", "shipment", "delivery", "payment", "alert"];

// ─── In-memory storage ───
const notifications = [];
const rateLimitMap = {}; // { ip: [{ timestamp }, ...] }
const startTime = Date.now();

// ─── BUG FIX 1 & 2: Add unhandled rejection handler ───
process.on('unhandledRejection', (reason, promise) => {
    console.error('[UNHANDLED REJECTION]', reason);
});

// ─── BUG FIX 2: Periodic cleanup of rate limit map ───
const cleanupInterval = setInterval(() => {
    const now = Date.now();
    for (const ip in rateLimitMap) {
        rateLimitMap[ip] = rateLimitMap[ip].filter(t => now - t < RATE_LIMIT_WINDOW);
        if (rateLimitMap[ip].length === 0) {
            delete rateLimitMap[ip];
        }
    }
}, 60000); // Cleanup every 60 seconds

/**
 * Generate a unique request ID or use X-Correlation-ID from headers
 */
function getRequestId(headers) {
  // Check for X-Correlation-ID from incoming request
  if (headers && headers['x-correlation-id']) {
    return headers['x-correlation-id'];
  }
  // Fall back to generated ID
  const timestamp = Date.now();
  const random = Math.random().toString(36).substring(2, 9);
  return `${timestamp}-${random}`;
}

/**
 * Parse request body with size limit
 */
function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    let bytes = 0;

    req.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > MAX_BODY_SIZE) {
        reject(new Error("BODY_TOO_LARGE"));
      } else {
        body += chunk;
      }
    });

    req.on("end", () => {
      try {
        resolve(JSON.parse(body || "{}"));
      } catch (e) {
        reject(new Error("INVALID_JSON"));
      }
    });

    req.on("error", reject);
  });
}

/**
 * Validate email format (simple regex)
 */
function isValidEmail(email) {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return emailRegex.test(email);
}

/**
 * Validate notification fields
 */
function validateNotification(body) {
  const errors = [];

  if (typeof body.orderId !== "string" || body.orderId.length === 0 || body.orderId.length > 50) {
    errors.push("orderId must be a string (1-50 chars)");
  }

  // ─── BUG FIX 4: Accept both customerEmail and customerId for backward compatibility ───
  const customerEmail = body.customerEmail || body.customerId || '';
  if (typeof customerEmail !== "string" || !isValidEmail(customerEmail)) {
    errors.push("customerEmail (or customerId) must be a valid email address");
  }

  if (typeof body.message !== "string" || body.message.length > 5000) {
    errors.push("message must be a string (max 5000 chars)");
  }

  if (typeof body.type !== "string" || !ALLOWED_NOTIFICATION_TYPES.includes(body.type)) {
    errors.push(`type must be one of: ${ALLOWED_NOTIFICATION_TYPES.join(", ")}`);
  }

  return errors;
}

/**
 * Rate limiter: check if request exceeds limit
 */
function isRateLimited(ip) {
  const now = Date.now();

  if (!rateLimitMap[ip]) {
    rateLimitMap[ip] = [];
  }

  // Remove old requests outside the window
  rateLimitMap[ip] = rateLimitMap[ip].filter(t => now - t < RATE_LIMIT_WINDOW);

  if (rateLimitMap[ip].length >= RATE_LIMIT_MAX) {
    return true;
  }

  // Add current request
  rateLimitMap[ip].push(now);
  return false;
}

/**
 * Add security headers to response
 */
function addSecurityHeaders(res) {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("X-Frame-Options", "DENY");
  res.setHeader("X-XSS-Protection", "1; mode=block");
  res.setHeader("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
}

/**
 * Send JSON response
 */
function send(res, statusCode, obj, requestId) {
  addSecurityHeaders(res);
  const json = JSON.stringify(obj);
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(json),
    "X-Request-ID": requestId,
  });
  res.end(json);
}

// ─── Create server ───
const server = http.createServer(async (req, res) => {
  const requestId = getRequestId(req.headers);
  const clientIp = req.headers["x-forwarded-for"] || req.socket.remoteAddress || "unknown";

  try {
    // ─── Health check endpoint ───
    if (req.method === "GET" && req.url === "/health") {
      const uptime = Date.now() - startTime;
      const memUsage = process.memoryUsage();
      return send(
        res,
        200,
        {
          status: "ok",
          uptime,
          memory: {
            heapUsed: Math.round(memUsage.heapUsed / 1024 / 1024 * 100) / 100, // MB
            heapTotal: Math.round(memUsage.heapTotal / 1024 / 1024 * 100) / 100,
          },
          notificationCount: notifications.length,
        },
        requestId
      );
    }

    // ─── List notifications with pagination ───
    if (req.method === "GET" && req.url.startsWith("/notifications")) {
      const url = new URL(req.url, `http://${req.headers.host}`);
      let limit = parseInt(url.searchParams.get("limit") || "50", 10);
      const offset = parseInt(url.searchParams.get("offset") || "0", 10);

      // Validate limit
      if (isNaN(limit) || limit < 1) limit = 50;
      if (limit > 100) limit = 100;

      const paginated = notifications.slice(offset, offset + limit);

      return send(
        res,
        200,
        {
          notifications: paginated,
          total: notifications.length,
          limit,
          offset,
        },
        requestId
      );
    }

    // ─── Post notification ───
    if (req.method === "POST" && req.url === "/notify") {
      // Check rate limit
      if (isRateLimited(clientIp)) {
        console.error(`[${requestId}] Rate limit exceeded for IP: ${clientIp}`);
        return send(res, 429, { error: "Too many requests" }, requestId);
      }

      // Check Content-Type
      const contentType = req.headers["content-type"] || "";
      if (!contentType.includes("application/json")) {
        console.error(`[${requestId}] Invalid Content-Type: ${contentType}`);
        return send(res, 415, { error: "Content-Type must be application/json" }, requestId);
      }

      // Parse body
      let body;
      try {
        body = await parseBody(req);
      } catch (e) {
        if (e.message === "BODY_TOO_LARGE") {
          console.error(`[${requestId}] Request body too large`);
          return send(res, 413, { error: "Request body too large" }, requestId);
        }
        if (e.message === "INVALID_JSON") {
          console.error(`[${requestId}] Invalid JSON in request body`);
          return send(res, 400, { error: "Invalid JSON" }, requestId);
        }
        throw e;
      }

      // Validate fields
      const validationErrors = validateNotification(body);
      if (validationErrors.length > 0) {
        console.error(`[${requestId}] Validation errors: ${validationErrors.join("; ")}`);
        return send(res, 400, { error: "Validation failed", details: validationErrors }, requestId);
      }

      // Store notification
      const customerEmail = body.customerEmail || body.customerId || '';
      const entry = {
        orderId: body.orderId,
        customerEmail: customerEmail,
        type: body.type,
        message: body.message,
        timestamp: body.timestamp || Date.now(),
        receivedAt: Date.now(),
        requestId,
      };
      notifications.push(entry);

      // ─── BUG FIX 1: Cap notifications array size to prevent memory leak ───
      if (notifications.length > MAX_NOTIFICATIONS) {
        notifications.splice(0, notifications.length - MAX_NOTIFICATIONS);
      }

      console.log(
        `[${requestId}] Notification received: orderId=${entry.orderId}, type=${entry.type}, email=${entry.customerEmail}`
      );

      return send(
        res,
        200,
        {
          ok: true,
          notificationId: notifications.length,
          requestId,
        },
        requestId
      );
    }

    // 404
    return send(res, 404, { error: "Not found" }, requestId);
  } catch (e) {
    // Log real error server-side
    console.error(`[${requestId}] Internal server error: ${e.message}`, e.stack);

    // Return generic error to client
    addSecurityHeaders(res);
    const json = JSON.stringify({ error: "Internal server error" });
    res.writeHead(500, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(json),
      "X-Request-ID": requestId,
    });
    res.end(json);
  }
});

// ─── Graceful shutdown ───
function gracefulShutdown(signal) {
  console.log(`\n[${signal}] Shutting down gracefully...`);
  clearInterval(cleanupInterval);
  server.close(() => {
    console.log("Server closed");
    process.exit(0);
  });

  // Force shutdown after 10 seconds
  setTimeout(() => {
    console.error("Forced shutdown due to timeout");
    process.exit(1);
  }, 10000);
}

process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
process.on("SIGINT", () => gracefulShutdown("SIGINT"));

// ─── Start server ───
const PORT = process.env.PORT || 5004;
server.listen(PORT, () => {
  console.log(`Notification service listening on port ${PORT}`);
});

module.exports = { server, notifications };
