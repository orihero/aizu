# Polar Sandbox Setup — Phase 4 e2e (products + webhook)

> Sandbox dashboard: **https://sandbox.polar.sh** (NOT polar.sh — sandbox is a
> fully separate environment with its own login, token, products, and webhook secret).
> Sandbox checkouts use Stripe **test cards** (e.g. `4242 4242 4242 4242`, any future
> expiry, any CVC) — no real money moves.

## Tier specs (from the billing plan §5)

Lead caps live in our code (`billing.TIERS`), **not** in Polar — Polar only holds the
price. You just create the price; the cap is already wired on our side.

| Tier | Monthly (USD) | Annual (USD) | Our lead cap |
|------|--------------:|-------------:|-------------:|
| Lite | $9.99 | $99 | 50 |
| Starter | $24.99 | $249 | 250 |
| Pro | $149 | $1,490 | 2,000 |

**Free** and **Scale** are NOT created in Polar (Free = implicit default, Scale = sales-led).

---

## Step 1 — Create the 6 products (2 per tier)

A Polar subscription product carries one recurring interval, so each tier needs a
**Monthly** product and an **Annual** product.

For **each** of the six rows below:

1. Sandbox dashboard → **Products** → **Create Product** (or **New Product**).
2. **Name** it exactly as in the table (so they're easy to tell apart later).
3. **Pricing** → **Recurring** → pick the **Billing cycle** (Monthly or Yearly).
4. **Amount** → currency **USD** → enter the amount from the table.
5. (Optional) description — not used by our code.
6. **Save / Create**.
7. **Copy the Product ID.** It's a UUID (looks like `xxxxxxxx-xxxx-…`). Find it on the
   product's page — either in the URL (`/products/<UUID>`) or a "Copy ID" / "…" menu.
   Paste each into the collector below as you go.

| Product name | Billing cycle | Amount | → paste Product ID here |
|--------------|---------------|-------:|-------------------------|
| AIZU Lite (Monthly) | Monthly | $9.99 | `LITE_MONTH = ` |
| AIZU Lite (Annual) | Yearly | $99.00 | `LITE_YEAR  = ` |
| AIZU Starter (Monthly) | Monthly | $24.99 | `STARTER_MONTH = ` |
| AIZU Starter (Annual) | Yearly | $249.00 | `STARTER_YEAR  = ` |
| AIZU Pro (Monthly) | Monthly | $149.00 | `PRO_MONTH = ` |
| AIZU Pro (Annual) | Yearly | $1,490.00 | `PRO_YEAR  = ` |

### Then assemble `POLAR_PRODUCTS`

Drop the 6 UUIDs into this one-line JSON and paste it into `.env` as `POLAR_PRODUCTS=`
(no spaces, no newlines):

```
{"lite":{"month":"LITE_MONTH","year":"LITE_YEAR"},"starter":{"month":"STARTER_MONTH","year":"STARTER_YEAR"},"pro":{"month":"PRO_MONTH","year":"PRO_YEAR"}}
```

> Our code validates at startup that all three tiers have BOTH month + year IDs and
> fails fast otherwise (`billing.py:_parse_products`), so a typo surfaces immediately.

---

## Step 2 — Organization Access Token

Sandbox dashboard → **Settings** → **Developers / API Keys** → **Create Token**
(an *Organization Access Token*; scopes: at least checkouts, customer sessions,
products read; "all" is fine for sandbox). Copy it once (`polar_oat_…`).
→ paste into `.env` as `POLAR_ACCESS_TOKEN=`.

---

## Step 3 — Webhook endpoint  (do this AFTER ngrok is up)

You start ngrok yourself (`ngrok http 8765`) and give me the public `https://…` URL.
Then:

1. Sandbox dashboard → **Settings** → **Webhooks** → **Add Endpoint**.
2. **URL** = `https://<your-ngrok-subdomain>.ngrok-free.app/api/billing/webhook`
   (note the exact path `/api/billing/webhook`).
3. **Format** = **Raw** (Standard Webhooks) — the default.
4. **Events**: subscribe to all `subscription.*`
   (`subscription.created`, `.updated`, `.active`, `.canceled`, `.revoked`,
   `.uncanceled`) plus `order.created` / `order.paid`. (Selecting "all events" is
   fine — we ack-and-ignore unknown types.)
5. **Save** → copy the **signing secret** (`whsec_…`).
   → paste into `.env` as `POLAR_WEBHOOK_SECRET=`.

---

## Result: `.env` should have

```
POLAR_SERVER=sandbox
POLAR_ACCESS_TOKEN=polar_oat_…
POLAR_WEBHOOK_SECRET=whsec_…
POLAR_PRODUCTS={"lite":{…},"starter":{…},"pro":{…}}
```

Tell me when these four are filled in and ngrok is running — I'll launch the
bridge+panel with `.env` sourced and we run the checkout → webhook → active flow.
