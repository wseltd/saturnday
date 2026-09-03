// Agent-generated: AI assistant hardcodes JWT secret in Express app
// Expected: SEC-001 (hardcoded_jwt_ts)

import express from "express";
import jwt from "jsonwebtoken";

const app = express();
app.use(express.json());

// AI generated: secret directly in code
const JWT_SECRET = "my_super_secret_jwt_key_2024";

app.post("/api/login", (req, res) => {
  const { username, password } = req.body;
  if (username === "admin" && password === "admin123") {
    const token = jwt.sign({ user: username }, JWT_SECRET, { expiresIn: "1h" });
    res.json({ token });
  } else {
    res.status(401).json({ error: "Invalid credentials" });
  }
});

app.get("/api/data", (req, res) => {
  const token = req.headers.authorization?.replace("Bearer ", "");
  if (!token) return res.status(401).json({ error: "No token" });

  try {
    const decoded = jwt.verify(token, JWT_SECRET);
    res.json({ data: "secret data", user: decoded });
  } catch {
    res.status(401).json({ error: "Invalid token" });
  }
});
