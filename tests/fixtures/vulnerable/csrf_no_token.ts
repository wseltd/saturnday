// Vulnerable: cookie-backed POST without CSRF protection
import express from "express";
import session from "express-session";

const app = express();
app.use(session({ secret: "key", cookie: { secure: true } }));

app.post("/api/transfer", (req, res) => {
  // State-changing with cookie session, no CSRF
  const { amount, to } = req.body;
  doTransfer(req.session.userId, to, amount);
  res.json({ status: "ok" });
});
