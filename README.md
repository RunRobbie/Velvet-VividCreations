# Velvet & Vivid Creations

**A runnable, full-stack website and private stylist management application.**

Designed for a one-stylist luxury hair artistry studio in Melbourne, Florida. The brand uses the approved **black background, champagne/rose-gold serif wordmark, and maroon velvet fabric on the left**, with **no owner name, no script lettering, no circular logos, and no hair photography in the main logo**.

The included image is based on the approved logo-banner direction. It is a website graphic, not yet a professionally typeset vector logo. Have a designer recreate the final mark as SVG for large-format printing.

## Updated layout and mobile preview (October 26, 2026, v1.2)

This package contains the **updated production website**, not only revised mockups.

- Header navigation now uses a compact **V & V** mark so the full banner logo is not repeated immediately underneath it.
- The footer has a single coherent design: a centered brand above two aligned link groups and a centered location section on phones; a balanced three-column navigation at tablet/desktop-mode widths; and four aligned brand/navigation columns on large desktop screens. **CREATIONS** remains centered under the full name, and the timezone is removed from the public footer.
- The decorative velvet panel has been cropped from the original graphic to remove stray lettering, and the split-image overlay has been removed.
- The mobile homepage displays an accessible, screen-sized serif brand treatment with the velvet positioned at the left. The desktop site retains the approved full-resolution hero banner.
- Styling was checked at 320, 390, 600, 768, 820, 980 (desktop mode on a phone), 1120, 1121, 1365, and 1920 pixels wide, across the homepage, services, gallery, booking, contact, story, and dashboard. The Services card layout was also corrected for narrow windows. No horizontal page overflow or client-side JavaScript errors remained. All five backend smoke tests passed.

The **Velvet-and-Vivid-Phone-Preview-v1.2.html** file included beside the application folder in the ZIP is a standalone offline preview. On Android, save the HTML file and open it in Chrome via Files/Downloads. It displays sample data, never saves changes, and does not represent a hosted application. The live website source is in the `velvet-vivid-creations` folder; its booking, customer records and billing require the Python server described below.

## Quick start (no dependencies)

Requires **Python 3.10+** (3.13 recommended). No package installation or Node build is needed.

### Windows (PowerShell)

1. Extract the ZIP to a folder such as `C:\Sites\velvet-vivid-creations`.
2. Open PowerShell in that folder.
3. Run:

   ```powershell
   py app.py
   ```
4. Open **http://localhost:8000** in a browser.
5. In a second PowerShell window, create the owner login:

   ```powershell
   py app.py create-admin --email owner@example.com --name "Studio Owner"
   ```

   Replace the example email with the real account email. You will be prompted for a password of **at least 12 characters**. No default admin password is provided.
6. Open **Account**, sign in, and use the **Stylist Portal**. The application immediately saves records in `data/salon.sqlite3`.

### macOS / Linux

```bash
python3 app.py
# In a second terminal:
python3 app.py create-admin --email owner@example.com --name "Studio Owner"
```

> You can create the admin account before starting the server too. To test the public booking flow, leave the server running while using your browser.

### Using the site

- Public website: `http://localhost:8000`
- Private dashboard: `http://localhost:8000/#/admin/overview` (login required)
- Client account: `http://localhost:8000/#/account`
- Booking calendar: `http://localhost:8000/#/book`
- Printable collateral: open `collateral/printable-brand-templates.html` locally in your browser.

**The service menu is intentionally sample data.** Edit it before launch in **Stylist Portal → Services**. Default working hours are Tuesday–Saturday, 9 AM–6 PM Eastern, editable in **Availability**. Other constraints: advance notice of 2 hours, 90-day booking horizon, 30-minute start increments. Booking windows account for the duration of each service and both pending and approved appointments.

## What works without outside providers

| Feature | Implementation |
|---|---|
| Public website | Home, services, gallery, story, contact, policies; mobile responsive |
| Booking | Real available time lookup; client details; booking request; concurrency-safe overlap rejection |
| Approval workflow | Pending → Approved / Declined / Cancelled / Completed / No-show |
| Calendar | Day, week and month; reschedule; unavailable-time blocks |
| Client accounts | Optional email/password signup, secure sign-in, profile updates, linked bookings, cancellations ≥24h in advance |
| Guest booking | Name, email, phone and optional SMS consent |
| Client management | Contacts, hair preferences, allergy/sensitivity notes, treatment formulas, visit history |
| Photos | Private upload and storage by default, consent-based public gallery, protected private media |
| Billing | Invoices, taxes as entered, discounts/coupons, payments, tips, balances, deposits via partial invoices |
| Payment methods | Manually record cash, external card reader, Zelle, Venmo, gift cards, other |
| Salon operations | Gift card issue and redemption, coupon creation, inventory levels, expense logging |
| Reporting | Revenue overview, outstanding balances and CSV exports |
| Inquiries | Messages saved in the private dashboard |
| Brand collateral | Printable business cards, gift certificate, next-visit card, consultation form, email preview |

Appointments, private records, and payments are persisted in a single on-disk SQLite database. Each booking is created in a write transaction to prevent two competing requests from reserving the same slot. Requests hold their slots until the stylist declines, cancels or completes them.

## Connect online payments (optional)

The site uses **Stripe Hosted Checkout**. It never collects or stores card numbers.

1. Create a Stripe merchant account and obtain its secret API key.
2. Set the `STRIPE_SECRET_KEY` environment variable on the hosting platform.
3. Add a Stripe webhook endpoint: `https://YOUR-DOMAIN/api/stripe/webhook` subscribing to **`checkout.session.completed`**.
4. Copy the webhook signing secret to `STRIPE_WEBHOOK_SECRET`.
5. Test with Stripe test-mode credentials and sample payments before switching to live mode.
6. In the **Billing** dashboard, create an invoice, then click **Stripe link**. Send the generated Checkout URL to the client. Only a valid, signed webhook marks an invoice paid.

A deposit can be handled as a separate invoice or a partial payment. The system does **not** automatically charge cancellation fees or hold cards on file. Any cancellation policy, cancellation invoice or deposit forfeiture needs your final business rules and client authorization.

## Connect email and SMS (optional)

Set `SMTP_HOST`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`, and optionally `SMTP_PORT` / `SMTP_STARTTLS` to enable email confirmations. Set `ADMIN_NOTIFY_EMAIL` to receive appointment and inquiry alerts.

Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM` to enable transactional SMS for clients who consented to appointment reminders. US mobile numbers entered as 10 digits are automatically converted to +1 E.164 format. Ensure sender verification, A2P 10DLC compliance where applicable, and proper opt-in records before enabling.

When a provider is configured, booking request/approval messages are sent immediately. A built-in scheduler checks for approved appointments roughly **24 hours ahead** every **30 minutes** and records deliveries to avoid normal duplicate sending. You can disable the in-process reminder loop with `ENABLE_REMINDER_LOOP=0` and instead call `python app.py reminders` from a scheduler (not both). Without providers, the booking state still works in the database, but **customers will not receive automated email/SMS**.

## Passwords and MFA

- Passwords are salted and hashed using `hashlib.scrypt`, not stored in plaintext.
- Sessions use long random tokens, stored as hashes in SQLite and delivered in `HttpOnly`, `SameSite=Lax` cookies. In HTTPS production deployments, cookies also use `Secure`.
- Account password reset requires configured SMTP and uses single-use, 30-minute reset tokens.
- **Staff TOTP is mandatory when `PUBLIC_BASE_URL` begins with `https://`.** Configure `ADMIN_TOTP_SECRET` before the first production login. Generate a secret:

  ```bash
  python -c "import base64,secrets;print(base64.b32encode(secrets.token_bytes(20)).decode())"
  ```

  Add it as a time-based OTP secret in your authenticator app. Keep it private. Set `REQUIRE_ADMIN_2FA=1` in production as an extra guard.

## Deployment (domain and hosting not yet selected)

Use a host with **managed HTTPS, a persistent filesystem/volume, backups, and one long-running app instance**. A reference `Dockerfile` and `render.yaml` are included; a paid host plan may be needed for persistent disk storage.

Typical Docker deployment, behind a platform TLS terminator:

```bash
# Build from this folder:
docker build -t velvet-vivid:1 .

# Mount a persistent location. The host or platform MUST serve the site over HTTPS:
docker run --rm -p 8000:8000 --env-file .env.production \
  -v /ABSOLUTE/HOST/PATH/salon-data:/data velvet-vivid:1
```

**Required production environment variables:**

```text
PUBLIC_BASE_URL=https://your-real-domain.com
DATA_DIR=/data
ADMIN_TOTP_SECRET=<your own base32 TOTP secret>
REQUIRE_ADMIN_2FA=1
PORT=8000
```

The app does not auto-load `.env.example`. Set environment variables through your deployment dashboard or load your own private env file. Do not publish passwords, API keys, the database, backup files or raw client photos with the website source.

### Why SQLite instead of PostgreSQL?

For a single owner and one app instance, SQLite provides a zero-dependency, reliable starting point with WAL and serialized writes. It **must** be stored on a durable disk and is **not suitable for stateless environments or horizontal scaling**. Do not deploy this ZIP as static files on GitHub Pages or as a read-only/serverless frontend: those platforms cannot run the secure server and persistent database as packaged. If you later add employees, multiple server instances or high traffic, migrate the data layer to a managed PostgreSQL service, migrate media to private object storage, and add centralized rate limiting.

## Backups and disaster recovery

Run `python app.py backup` regularly. This creates a consistent timestamped SQLite backup. **The uploads directory must also be backed up**; it is not part of the SQLite file. Schedule encrypted copies of the DB and uploads to *another* storage service, test restores regularly, and ensure backups are access-controlled. Backups kept only on the same disk do not protect against host failure.

## Security and privacy launch checklist

- [ ] Configure the actual domain and HTTPS; verify `PUBLIC_BASE_URL` exactly matches the browser origin.
- [ ] Set owner TOTP MFA and a strong unique admin password; never share credentials.
- [ ] Confirm the hosting volume is persistent, private and included in automated offsite backups.
- [ ] Configure a trustworthy edge reverse proxy with request/body size limits and additional DDoS/rate protection. `TRUST_PROXY_IP_HEADERS=1` is only safe if that proxy **overwrites** incoming `X-Forwarded-For` headers.
- [ ] Set up and test real transactional email, SMS consent, and Stripe webhooks before enabling those features.
- [ ] Replace sample service descriptions, prices, hours, and policy language with approved real information.
- [ ] Collect verifiable written client photo releases before marking any client photo as publicly shareable. Revoking consent hides the photos from public responses, but copies already distributed to other platforms may persist.
- [ ] Determine record retention, consent evidence, privacy notices, deletion/export procedures, tax treatment, cancellation terms and state/local business obligations with qualified advisers.
- [ ] Test customer and stylist flows across desktop and mobile browsers, especially on the actual domain; review accessibility and conduct security testing before public launch.
- [ ] Use backups, patch the host Python image regularly, monitor errors, and create an incident-response plan.

**Built-in protections:** server-side role authorization, same-origin/CSRF checks, HTML encoding, restricted media access, file size/type checks, HTTPS security headers, password hashing, owner MFA support, IP login/booking throttles, Stripe webhook signature validation, and database-level overlap prevention.

**Not a finished compliance certification:** This is a complete runnable starter application, not a representation that it has been independently penetration-tested, audited for specific state privacy laws, or approved for PCI/HIPAA/legal compliance. Stripe handles card data; medical-like allergy/sensitivity notes deserve careful access control and appropriate privacy processes.

## Branding / assets

- Hero asset: `public/velvet-vivid-hero.webp`
- Maroon-velvet accent: `public/velvet-texture.webp`
- Browser favicon: `public/favicon.svg`
- Colors: Black `#140c10`, Burgundy `#37111f`, Champagne `#d9ad8f`, Rose `#c78f7c`, Ivory `#f8f3ed`.
- No personal stylist name is present in the design.
- Printed collateral templates: `collateral/printable-brand-templates.html`.

## Tests

```bash
python -m unittest discover -s tests -v
```

The included smoke tests cover secure access, account signup and login, guest booking, booking collisions, stylist approval, invoices, gift cards, client notes, and CSRF protection. They do not replace manual browser review and payment/email provider sandbox testing.

## Project structure

```text
app.py                 Backend, authentication, booking, database, payments and alerts
index.html             Public website / private dashboard shell
site.js                Website pages, booking and dashboard interactions
styles.css             Responsive styling and luxury brand design
public/                Logo and velvet assets, favicon
collateral/             Printable stationery templates
Dockerfile              Deployable single-container app
render.yaml             Example managed host deployment with durable disk
.env.example           Configuration field names (no real credentials)
tests/                 Automated tests
data/                  SQLite database and protected uploads (NOT for publication)
```

## Important limitations

The initial application includes all core workflows, but **publication still requires a domain, hosting, real owner login creation, final salon policies, and integrations**. It does not include a legal/privacy certification, PCI audit, full point-of-sale terminal integration, automated tax filing, full inventory cost-of-goods accounting, guaranteed message delivery, or real historical client photos. Coupon codes apply to manually issued invoices, not to the initial request form. Photos require the salon to document permission. All example prices are placeholders. Changes to the business name can be made throughout `app.py`, `index.html`, and `site.js`, and you should replace the logo image if rebranding.
