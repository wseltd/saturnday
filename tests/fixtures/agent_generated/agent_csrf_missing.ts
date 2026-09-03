// Agent-generated: AI assistant creates POST routes without CSRF protection
// Expected: SEC-009 (csrf_state_change_ts)

import express from "express";

const app = express();
app.use(express.json());

// No CSRF middleware applied

app.post("/api/transfer", (req, res) => {
  // AI generated: state-changing operation without CSRF token
  const { from, to, amount } = req.body;
  res.json({ status: "transferred", amount });
});

app.post("/api/settings", (req, res) => {
  // AI generated: settings update without CSRF protection
  const settings = req.body;
  res.json({ status: "updated", settings });
});

app.post("/api/password", (req, res) => {
  // AI generated: password change without CSRF token validation
  const { oldPassword, newPassword } = req.body;
  res.json({ status: "password changed" });
});

app.put("/api/profile", (req, res) => {
  // AI generated: profile update via PUT without CSRF
  res.json({ status: "profile updated" });
});
