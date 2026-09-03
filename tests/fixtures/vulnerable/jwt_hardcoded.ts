// Vulnerable: hardcoded JWT secrets in TypeScript
import jwt from "jsonwebtoken";

const JWT_SECRET = "super-secret-key-123";
const SIGNING_KEY = "my-signing-key";

// Fallback default
const TOKEN_SECRET = process.env.TOKEN_SECRET || "fallback-secret";

export function createToken(userId: string): string {
  return jwt.sign({ userId }, "hardcoded-inline-secret", { expiresIn: "1h" });
}
