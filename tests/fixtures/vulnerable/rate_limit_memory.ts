// Vulnerable: rate limiter with default in-memory store
import rateLimit from "express-rate-limit";

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 100,
});

// Note: no external store configured — uses default memory store
