// Vulnerable: SQL in template literal
export function searchUsers(query: string) {
  const sql = `SELECT * FROM users WHERE name LIKE '%${query}%'`;
  return db.execute(sql);
}
