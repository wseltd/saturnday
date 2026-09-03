// Vulnerable: rate limiter imported but never applied to routes
import rateLimit from "express-rate-limit";

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 100,
});

// limiter is never passed to app.use() or any route
import express from "express";
const app = express();

app.get("/api/data", (req, res) => {
  res.json({ data: "no rate limit here" });
});
