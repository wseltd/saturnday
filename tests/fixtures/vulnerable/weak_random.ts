// Vulnerable: Math.random for auth-adjacent contexts
export function generateResetToken(): string {
  return Math.random().toString(36).substring(2);
}

export function createInviteCode(): string {
  const code = Math.floor(Math.random() * 1000000);
  return code.toString().padStart(6, "0");
}
