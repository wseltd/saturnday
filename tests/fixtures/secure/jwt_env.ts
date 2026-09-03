// Secure: JWT secrets from environment, no fallback
import jwt from "jsonwebtoken";

const JWT_SECRET = process.env.JWT_SECRET!;

export function createToken(userId: string): string {
  return jwt.sign({ userId }, JWT_SECRET, { expiresIn: "1h" });
}

// Non-secret constant (should NOT flag)
const APP_NAME = "my-application";
