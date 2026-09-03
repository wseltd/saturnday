// Vulnerable: JWT sign without expiresIn
import jwt from "jsonwebtoken";

export function createToken(userId: string) {
  return jwt.sign({ userId }, process.env.SECRET!);
}
