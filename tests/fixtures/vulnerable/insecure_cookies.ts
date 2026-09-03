// Vulnerable: insecure cookie settings
import express from "express";

const app = express();

app.get("/set-session", (req, res) => {
  // Bad: SameSite=None without secure, no httpOnly
  res.cookie("session", "token123", {
    sameSite: "none",
  });
  res.json({ status: "ok" });
});

app.get("/set-refresh", (req, res) => {
  // Bad: no httpOnly on refresh token
  res.cookie("refresh_token", "refresh123", {
    domain: ".example.com",
  });
  res.json({ status: "ok" });
});
