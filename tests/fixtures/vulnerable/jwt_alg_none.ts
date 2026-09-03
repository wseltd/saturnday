// Vulnerable: JWT verify without pinning algorithms
import jwt from "jsonwebtoken";

export function verifyToken(token: string) {
  // Bad: no algorithms option — attacker can choose
  return jwt.verify(token, "secret-key");
}

export function verifyTokenNoneAllowed(token: string) {
  // Bad: alg=none explicitly allowed
  return jwt.verify(token, "secret-key", { algorithms: ["HS256", "none"] });
}
