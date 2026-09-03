// Vulnerable: logout without token invalidation
export function logout(req, res) {
  // No token blacklisting or session destruction
  res.json({ message: "Logged out" });
}

export function resetPassword(req, res) {
  const { newPassword } = req.body;
  updatePassword(req.user.id, newPassword);
  // No token revocation
  res.json({ message: "Password reset" });
}
