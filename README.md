# Private Lessons with Coach Dacia

A mobile booking app for private gymnastics lessons. Parents request a lesson without an account and pay through Venmo. The coach confirms the appointment after verifying the payment in Venmo. Stripe Checkout remains available if configured later. The coach dashboard is at `/admin`.

## Local preview

Requires Python 3.10 or newer. Run `python3 app.py`, then open `http://127.0.0.1:8000`. The local server creates `bookings.sqlite3` automatically. Set `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and `MANAGE_LINK_SECRET` in your shell to use the admin and booking management flows. For Venmo requests, set `PAYMENT_METHOD=venmo` and `VENMO_USERNAME`; the coach marks each payment received in the dashboard.

The 30-minute lesson starts at $40. The 40-minute lesson starts at $53 and is described as best for learning two skills. Prices, buffer time, availability, blocked times, and policies can be changed in the dashboard.

## Supabase and Vercel deployment

1. Open your Supabase project, then **SQL Editor**. Run the entire [supabase_schema.sql](supabase_schema.sql) file. It creates the database tables, starts with the $40 and $53 prices, and includes the Saturday 3:00–5:10 PM availability and the six October 25, 2026 blocked times from the local dashboard. The script is safe to run again without duplicating those rules.
2. In Supabase **Connect**, copy the **Transaction pooler** URI (port 6543). Use that complete URI as Vercel's `DATABASE_URL` environment variable. Keep it server-side. The app uses SSL and disables prepared statements for the transaction pooler.
3. Publish this folder to a private GitHub repository using GitHub Desktop. `.gitignore` excludes the local database, `.env`, and virtual environment.
4. In Vercel, import that GitHub repository. Use the repository root and the **Flask** framework preset. The `app.py` Flask entry point and `requirements.txt` are ready for Vercel. Add the environment variables listed below to **Production** before enabling bookings. Set `PUBLIC_BASE_URL` to your final `https://...` Vercel or custom domain, without a trailing slash.
5. Check that the Venmo profile link is correct. New requests appear as **Awaiting payment** in the coach dashboard. Verify the payment in Venmo, then click **Mark Venmo paid** to confirm the lesson. Pending requests hold their time until confirmed or declined.
6. Verify `/api/config`, `/book`, and `/admin` on the deployed domain. Submit a test request, verify it stays pending, then decline it from the coach dashboard to release the time.

### Required Vercel environment variables

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Supabase Transaction pooler URI; include `sslmode=require`. |
| `BUSINESS_TIMEZONE` | Usually `America/Los_Angeles`. |
| `PUBLIC_BASE_URL` | Final HTTPS site origin. |
| `ADMIN_EMAIL` | Email address for coach login. |
| `ADMIN_PASSWORD` | Long unique coach login password. |
| `MANAGE_LINK_SECRET` | Separate long random string used for secure parent links. Keep stable across deployments. |
| `PAYMENT_METHOD` | Set to `venmo` for manual payment verification, or `stripe` for Stripe Checkout. |
| `VENMO_USERNAME` | Public Venmo username used when `PAYMENT_METHOD=venmo`. |
| `STRIPE_SECRET_KEY` | Required only for Stripe mode. |
| `STRIPE_WEBHOOK_SECRET` | Required only for Stripe mode. |
| `CONTACT_EMAIL` | Email shown when online changes are closed. |

Optional email confirmation and reminder variables: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `EMAIL_FROM`. Confirmation emails are attempted after the coach marks a Venmo payment received or after a Stripe webhook. The local reminder command is `python3 app.py --send-reminders`; a hosted schedule is not configured yet.

## Data and policies

`parents` can have multiple `gymnasts`. `bookings` link parents, gymnasts, lesson types, and a version of the policies accepted before payment. `payments` and `webhook_events` match Stripe transactions and prevent duplicate webhook processing. Appointment times are stored in UTC; weekly availability uses the configured business time zone. The server serializes booking writes to prevent double booking. No Stripe secret or Supabase database URI is sent to the browser.

The local SQLite file is for development. Vercel uses Supabase Postgres through `DATABASE_URL`; it never relies on a writable local file. If you already have real bookings in SQLite, migrate them before accepting bookings through Vercel. This local file currently has no parent or booking records.
