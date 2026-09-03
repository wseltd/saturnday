// Vulnerable: auth handler with only console.log
export async function login(req, res) {
  const user = await authenticate(req.body.username, req.body.password);
  if (!user) {
    console.log("login failed");
    return res.status(401).json({ error: "Invalid credentials" });
  }
  return res.json({ token: createToken(user.id) });
}
