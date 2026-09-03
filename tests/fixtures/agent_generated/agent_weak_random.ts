// Agent-generated: AI assistant uses Math.random() for security tokens
// Expected: SEC-006 (weak_randomness_ts)

import express from "express";

const app = express();

function generateApiKey(): string {
  // AI generated: Math.random() for API key generation
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = "";
  for (let i = 0; i < 32; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

function generateResetToken(): string {
  // AI generated: Math.random() for password reset token
  return Math.random().toString(36).substring(2, 15) +
    Math.random().toString(36).substring(2, 15);
}

app.post("/api/keys", (req, res) => {
  const key = generateApiKey();
  res.json({ apiKey: key });
});

app.post("/api/reset-password", (req, res) => {
  const token = generateResetToken();
  res.json({ resetToken: token });
});
