// Agent-generated: AI assistant sets cookies without security attributes
// Expected: SEC-005 (cookie_security_hard_ts)

import express from "express";

const app = express();
app.use(express.json());

app.post("/login", (req, res) => {
  const { username } = req.body;
  // AI generated: session cookie without secure flags
  res.cookie("session_id", "abc123");
  res.cookie("auth_token", "xyz789", { httpOnly: false });
  res.json({ status: "logged in", user: username });
});

app.post("/preferences", (req, res) => {
  // AI generated: cookie with SameSite=None but no Secure flag
  res.cookie("prefs", "dark_mode=true", { sameSite: "none" });
  res.json({ status: "preferences saved" });
});

app.get("/track", (req, res) => {
  // AI generated: tracking cookie with no security attributes
  res.cookie("tracking_id", "track_" + Date.now());
  res.json({ status: "tracked" });
});
