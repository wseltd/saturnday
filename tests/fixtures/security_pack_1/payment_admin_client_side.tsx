// Fixture: SEC-FE-001 + SEC-FE-002 — Payment admin ops with hardcoded secret in TSX
// This is intentionally vulnerable for Saturnday proof tests.

import React from "react";

// SEC-FE-002: Stripe secret key hardcoded in a .tsx frontend file.
const STRIPE_SECRET = "SATURNDAY_TEST_SK_TOKEN";

interface RefundRequest {
  chargeId: string;
  amount: number;
}

// Client-side refund/charge using a hardcoded secret — plainly wrong.
async function issueRefund(req: RefundRequest): Promise<void> {
  await fetch(`https://api.stripe.com/v1/refunds`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${STRIPE_SECRET}`,
      "Content-Type": "application/x-www-form-urlencoded",
    },
    body: `charge=${req.chargeId}&amount=${req.amount}`,
  });
}

async function createCharge(amount: number, source: string): Promise<void> {
  await fetch("https://api.stripe.com/v1/charges", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${STRIPE_SECRET}`,
    },
    body: new URLSearchParams({ amount: String(amount), source }),
  });
}

export { issueRefund, createCharge };
