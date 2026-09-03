// Vulnerable: differentiated login errors
export async function login(username: string, password: string) {
  const user = await findUser(username);
  if (!user) {
    return { error: "User not found", status: 404 };
  }
  if (!checkPassword(user, password)) {
    return { error: "Wrong password", status: 401 };
  }
  return { token: createToken(user.id), status: 200 };
}
