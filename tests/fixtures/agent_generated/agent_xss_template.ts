// Agent-generated: AI assistant uses innerHTML with user input
// Expected: SEC-013 (xss_check_ts)

import express from "express";

const app = express();

app.get("/search", (req, res) => {
  const query = req.query.q || "";
  // AI generated: innerHTML assignment with user-controlled data
  document.getElementById("results").innerHTML = `<h1>Results: ${query}</h1>`;
  res.send("ok");
});

app.get("/profile/:username", (req, res) => {
  const { username } = req.params;
  // AI generated: innerHTML assignment
  document.getElementById("profile").innerHTML = '<h1>' + username + '</h1>';
  res.send("ok");
});

// AI generated: dangerouslySetInnerHTML in React component
function SearchResults({ query }: { query: string }) {
  return <div dangerouslySetInnerHTML={{ __html: query }} />;
}
