// Agent-generated: AI assistant uses template literals in SQL queries
// Expected: SEC-015 (sql_injection_ts)

import express from "express";

const app = express();
const db: any = {};  // placeholder

app.get("/api/users/search", async (req, res) => {
  const query = req.query.q as string;
  // AI generated: template literal SQL injection
  const sql = `SELECT * FROM users WHERE name LIKE '%${query}%'`;
  const results = await db.query(sql);
  res.json(results);
});

app.get("/api/users/:id", async (req, res) => {
  const id = req.params.id;
  // AI generated: string concatenation in SQL
  const result = await db.query("SELECT * FROM users WHERE id = " + id);
  res.json(result);
});

app.get("/api/orders", async (req, res) => {
  const sortBy = req.query.sort || "created_at";
  // AI generated: user input in ORDER BY clause
  const sql = `SELECT * FROM orders ORDER BY ${sortBy}`;
  const results = await db.query(sql);
  res.json(results);
});
