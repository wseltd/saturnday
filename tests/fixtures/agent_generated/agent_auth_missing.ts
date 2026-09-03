// Agent-generated: AI assistant creates API routes without auth middleware
// Expected: SEC-002 (auth_bypass_ts)

import express from "express";

const app = express();
app.use(express.json());

const users: Record<string, any> = {};

// No auth middleware applied — all routes publicly accessible
app.get("/api/users", (req, res) => {
  res.json(Object.values(users));
});

app.delete("/api/users/:id", (req, res) => {
  delete users[req.params.id];
  res.json({ status: "deleted" });
});

app.post("/api/admin/config", (req, res) => {
  // Admin config endpoint with no authentication
  res.json({ status: "config updated", config: req.body });
});

app.post("/api/admin/reset", (req, res) => {
  // Dangerous reset operation with no auth
  Object.keys(users).forEach((k) => delete users[k]);
  res.json({ status: "database reset" });
});
