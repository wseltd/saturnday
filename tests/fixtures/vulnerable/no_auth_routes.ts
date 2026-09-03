// Vulnerable: Express routes without auth middleware
import express from "express";

const app = express();

app.get("/api/users/:userId", (req, res) => {
  // No auth middleware
  res.json(getUserData(req.params.userId));
});

app.post("/api/admin/settings", (req, res) => {
  // No auth on admin route
  res.json({ status: "updated" });
});

// Public route (should NOT flag)
app.get("/health", (req, res) => {
  res.json({ status: "ok" });
});
