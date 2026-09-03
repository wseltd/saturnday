// Fixture: SEC-FE-001 + SEC-FE-002 — Stripe secret key in React component
// This is intentionally vulnerable for Saturnday proof tests.

import React, { useState } from "react";

const STRIPE_SECRET = "SATURNDAY_TEST_SK_TOKEN";

export default function CheckoutForm({ cartTotal }) {
  const [price, setPrice] = useState(cartTotal);

  const handleCharge = async () => {
    // SEC-FE-002: Secret key embedded in frontend bundle — never safe.
    const response = await fetch("/api/charge", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${STRIPE_SECRET}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ amount: price }),
    });
    return response.json();
  };

  return (
    <div>
      <button onClick={handleCharge}>Pay ${price}</button>
    </div>
  );
}
