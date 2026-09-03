// Vulnerable: secret keys exposed in frontend/browser-delivered code

// Bad: Stripe secret key on the client
const STRIPE_SECRET_KEY = "SATURNDAY_TEST_SK_TOKEN";

// Bad: direct Stripe charge from frontend (should be server-side)
async function chargeCustomer(amount: number) {
  const response = await fetch("https://api.stripe.com/v1/charges", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${STRIPE_SECRET_KEY}`,
    },
    body: JSON.stringify({ amount }),
  });
  return response.json();
}

// Bad: generic secret exposed
const API_SECRET = "secret_abc123def456";
const ADMIN_TOKEN = "tok_live_xxxxxxxxxxxxxxxx";

export { chargeCustomer };
