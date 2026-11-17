# Passwordless login and cross-device settings

Login is optional. Anonymous visitors keep every preference in the browser's local storage and the
app never contacts the login endpoints for them. Signing in adds a server-side copy of the same
preferences so they follow the account to another browser or device.

The flow deliberately avoids passwords and password hashes: a user proves control of an email
address with a short-lived one-time code or a magic link, and the service only stores opaque,
hashed session tokens.

## User flow

1. **Anmelden** in the filter bar opens the account dialog.
2. The user enters an email address. The backend sends one email that contains both a six-digit
   code and a magic link. The dialog then asks for the code.
3. The code can be typed on any device. On a second device the user enters the same email, chooses
   **Ich habe bereits einen Code**, and types the code without sending a new email. The link opens
   the app with `?email_token=…`; the frontend immediately removes the token from the URL, verifies
   it, and stores the session.
4. Either variant consumes the pending login, so the same email cannot be used twice and the other
   variant stops working.
5. While signed in, the account button shows the email address. The dialog offers **Abmelden** and
   **Konto löschen**.

Signing out clears the local session but keeps the browser's local preferences. Deleting the
account removes the server-side account, sessions, pending logins, and stored settings; the local
preferences stay in the browser and the app continues to work anonymously.

## Settings that are synchronized

- Selected and known series (`series_selection`)
- Per-platform preview preferences (`preview_preferences`)
- Seen/watched episodes (`seen_episode_ids`)

The client compares the server state with the local state after login:

- A **fresh account** (`version == 0`) receives the current local state, so signing in does not lose
  existing filters or watched markers.
- An **account with settings** (`version > 0`) wins: the browser adopts the server state.
- Changes are saved after a short idle period. Every write carries the last known `version`.
- If another device wrote first, the API answers `409 Conflict` with the current server version.
  The client adopts that version once and retries, so the local change still wins locally while the
  server version advances.

A session expiring or being revoked clears the local session and shows a notice; the app falls back
to anonymous/local mode.

## API

All endpoints are JSON and live under `/api/v1`. Authenticated requests use
`Authorization: Bearer <session token>`.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/auth/email/start` | – | Send code + link. Always answers `202 {"status": "sent"}` |
| `POST` | `/auth/email/verify` | – | Exchange `{email, code}` for a session |
| `POST` | `/auth/link/verify` | – | Exchange `{token}` from the magic link for a session |
| `POST` | `/auth/logout` | bearer | Delete the current session |
| `GET` | `/me` | bearer | Return the account id and email |
| `GET` | `/me/settings` | bearer | Return `{data, version, updated_at}` |
| `PUT` | `/me/settings` | bearer | Write `{data, version?}`; `409` on a stale version |
| `DELETE` | `/me` | bearer | Delete the account and all account data |

Unknown code, expired code, wrong code, and a consumed link all return a generic `400` message.
`/auth/email/start` never reveals whether an account already exists; it returns the same status and
body for known and unknown addresses. Delivery failures return `502`, but no account information.

Settings are validated and bounded: at most 5,000 IDs per series list (`selected` and `known` are
limited separately), 20,000 seen-episode IDs, and 100 platform preferences, each ID between 1 and
100 characters. Unknown nested fields are rejected (`422`).

## Data model

Four tables back the feature (migration `20261116_08_add_auth_tables`):

- `users` — `id` (UUID), unique normalized `email`, `email_verified_at`, `created_at`. The email is
  only an identity; settings are keyed by the UUID so changing the key later does not touch it.
- `login_tokens` — one row per requested login: email, scrypt-hashed code + salt, SHA-256 hash of
  the link token, attempts left, requesting IP, `used_at`, `expires_at`.
- `user_sessions` — SHA-256 hash of the session token as the primary key, user, optional user
  agent, timestamps, expiry.
- `user_settings` — one row per user: JSON document, monotonically increasing `version`,
  `updated_at`.

## Security decisions

- **No passwords and no password hashes.** Only email addresses and login artifacts are stored.
- **Codes** are 6 digits, valid for 10 minutes, limited to 5 attempts, and single-use. They are
  stretched with stdlib `scrypt` (`n=2^14`, `r=8`, `p=1`) and a random per-token salt, so a
  database dump does not expose them directly. A wrong attempt decrements the counter; the fifth
  locks the login.
- **Link and session tokens** are 32 random bytes (`secrets.token_urlsafe(32)`). Only their SHA-256
  digest is stored, so a leaked database cannot be replayed as a session.
- **Sessions** last 30 days. `last_seen_at` is refreshed at most once per hour to avoid a write on
  every request.
- **Rate limits** apply per email and per client IP: a 60-second cooldown per email, 5 requests per
  email per hour, and 20 per IP per hour. Requests over the limit silently return `202`, matching
  the anti-enumeration behavior and making brute-force spraying slow. The client IP comes from
  `X-Forwarded-For` when a proxy provides it.
- **New logins invalidate pending ones** for the same address, so only the newest email works.
- **Account deletion** removes pending login tokens for the address as well, so an already
  delivered link cannot recreate the account.

This is intentionally simple user management: no roles, no admin UI, no email change flow. To use a
different address, delete the account and sign in again.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BREVO_API_KEY` | empty | Brevo API key. Empty means "log the email instead of sending" |
| `AUTH_EMAIL_FROM` | `Episode Calendar <login@example.com>` | Sender as `Name <address>`; the address must be a verified Brevo sender |
| `AUTH_LINK_BASE_URL` | `http://localhost:5173` | Base URL used to build the magic link |

Tuning values live in `src/episode_calendar/api/config.py`: code TTL, cooldown, hourly limits, and
session TTL. The defaults are suitable for a small private deployment.

### Local development without a mail account

Without `BREVO_API_KEY`, `send_login_email` writes the code and link to the application log and
returns success. No Brevo account or internet access is needed:

```shell
uv run uvicorn episode_calendar.api.main:app --reload
# request a login, then read the code from the log output or open the logged link
```

`AUTH_LINK_BASE_URL` must be the frontend origin, because the link is opened by the browser.

## Email delivery with Brevo

Login mail goes through the [Brevo](https://www.brevo.com) transactional API
(`POST https://api.brevo.com/v3/smtp/email`, authenticated with an `api-key` header). Brevo is used
because it works **without owning a domain**: the sender is verified by an email Brevo sends to that
address, not by DNS records. The free plan covers 300 emails per day without a card or paid plan.
One login attempt sends exactly one email.

One-time setup:

1. Create a free Brevo account. No domain and no credit card are required.
2. Add the address you want as the sender under **Senders, Domains & Dedicated IPs → Senders**
   (older dashboards: **Senders & IP**) and click the confirmation link Brevo mails to it. Any
   mailbox you control works, including a freemail address.
3. Optional but recommended: authenticate a domain if you ever get one. With a verified sender only,
   Brevo still sends, but mailbox providers treat mail from shared infrastructure with less trust.
4. Create an API key with sending permission and store it as `BREVO_API_KEY` (locally in `.env`, in
   production in Secret Manager).
5. Set `AUTH_EMAIL_FROM` to that verified sender, for example
   `Episode Calendar <login@example.com>`.

Because the `From` domain is not authenticated, some providers file these mails as spam. The login
code is therefore part of the subject line, so it stays readable in a spam folder preview. Keeping
the sending volume low — which the per-email and per-IP rate limits already enforce — keeps the
shared-IP reputation stable.

**Cost.** The free plan is 300 emails per day. Exceeding it makes new login emails fail with `502`;
the app keeps working anonymously and existing sessions stay valid, so there is no paid fallback and
no additional cost.

## Cloud deployment

The complete first-deployment sequence lives in the README:
[step 1](../README.md#step-1--set-up-the-login-mail-sender-at-brevo),
[step 6](../README.md#step-6--store-the-secrets), and
[step 10](../README.md#step-10--verify-the-first-deployment) cover the mail sender, its secret, and
its verification. In short, Terraform creates an empty `google_secret_manager_secret` named by
`brevo_api_key_secret` and passes `AUTH_EMAIL_FROM` / `AUTH_LINK_BASE_URL` to Cloud Run. Add the key
version during the one-time setup, without putting it in a file or a command-line argument:

```shell
read -r -s BREVO_API_KEY; echo
printf '%s' "$BREVO_API_KEY" | gcloud secrets versions add episode-calendar-brevo-api-key --data-file=-
unset BREVO_API_KEY
```

Set `auth_email_from` and `auth_link_base_url` (the Firebase Hosting origin) in
`infra/terraform.tfvars`, then apply Terraform. The runtime service account already gets access to
the new secret through the existing Secret Manager IAM binding. Because the Cloud Run service
references the secret's `latest` version, create that version before applying with
`deploy_cloud_run = true`; otherwise the revision cannot start. Local runs without
`BREVO_API_KEY` log the login code instead of sending it.
