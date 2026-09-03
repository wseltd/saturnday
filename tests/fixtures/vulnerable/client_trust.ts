// Vulnerable: client-supplied authority fields
import express from "express";

const app = express();

app.post("/api/score", (req, res) => {
  const score = req.body.score;
  saveScore(req.user.id, score);
  res.json({ status: "ok" });
});

app.put("/api/user/role", (req, res) => {
  const role = req.body.role;
  updateUserRole(req.user.id, role);
  res.json({ status: "ok" });
});
