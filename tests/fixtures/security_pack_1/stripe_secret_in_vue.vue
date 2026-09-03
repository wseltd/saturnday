<!-- Fixture: SEC-FE-001 + SEC-FE-002 — Stripe secret key in Vue SFC -->
<!-- This is intentionally vulnerable for Saturnday proof tests.  -->

<template>
  <div class="payment">
    <button @click="charge">Charge Customer</button>
  </div>
</template>

<script>
// SEC-FE-002: Stripe sk_live_ in a .vue SFC — will be bundled into browser asset.
const STRIPE_SECRET = "SATURNDAY_TEST_SK_TOKEN";

export default {
  name: "PaymentForm",
  methods: {
    async charge() {
      const res = await fetch("https://api.stripe.com/v1/charges", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${STRIPE_SECRET}`,
        },
        body: new URLSearchParams({ amount: "2000", currency: "usd" }),
      });
      return res.json();
    },
  },
};
</script>
