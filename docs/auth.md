# Passkey login and cross-device settings

Login is optional. Anonymous visitors keep every preference in the browser's local storage and the
app never contacts the login endpoints for them. Signing in adds a server-side copy of the same
preferences so they follow the user to another browser or device.

Authentication uses passkeys (WebAuthn). There are no passwords, no password hashes, no email
addresses, and no mail provider. The server stores a public key per passkey, a credential ID, a
signature counter, and technical timestamps. Nothing in the database identifies a person. A database
dump cannot be used to sign in.

## User flow

1. **Anmelden** in the filter bar opens the account dialog.
2. A new user chooses **Neuen Passkey erstellen**. The browser or password manager creates a
   discoverable passkey for this site; the user confirms with Face ID, fingerprint, PIN, or hardware
   key. The dialog shows a randomly generated **Recovery-Code** once — it has to be stored safely.
3. On another device, **Mit Passkey anmelden** opens the browser's passkey selection. No username or
   email address is needed: the credential is discovered from the relying party ID.
4. If all devices are lost, **Recovery-Code eingeben** signs in and immediately issues a new code;
   the old one stops working. The old code is only shown until the dialog is closed.
5. While signed in, the account dialog lists all passkeys, allows adding and removing credentials,
   rotating the recovery code, signing out, and deleting the account.

Signing out clears the local session but keeps the browser's local preferences. Deleting the account
removes the server-side account, passkeys, recovery code, sessions, and stored settings; the local
preferences stay in the browser and the app continues to work anonymously.

## Multi-device behavior

| Situation | Result |
| --- | --- |
| Apple devices with the same Apple ID | Passkey syncs through iCloud Keychain |
| Android/Chrome with the same Google account | Passkey syncs through Google Password Manager |
| 1Password, Bitwarden, or similar | Cross-platform sync inside the password manager |
| Different ecosystems without a password manager | Add a second passkey on the new device, or use the recovery code once and create one there |

Multiple passkeys per account are supported. Removing one credential does not affect the others; a
user can remove all of them and still get back in with the recovery code.

Passkeys are bound to the **relying party ID**, i.e. the frontend hostname. Moving the frontend to a
different domain invalidates all existing passkeys and every user has to register again. Decide the
final hostname before rolling this out. WebAuthn also requires HTTPS (or `localhost` for local
development).

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
`Authorization: Bearer <session token>`. The `credential` object is the browser's
`PublicKeyCredential.toJSON()` output with base64url-encoded binary fields.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/auth/registration/options` | – | Start a new account; returns creation options |
| `POST` | `/auth/registration/verify` | – | Verify the attestation; returns session + recovery code |
| `POST` | `/auth/authentication/options` | – | Start a discoverable-credential assertion |
| `POST` | `/auth/authentication/verify` | – | Verify the signature; returns a session |
| `POST` | `/auth/passkeys/options` | bearer | Creation options for another passkey |
| `POST` | `/auth/passkeys` | bearer | Add a passkey; returns its metadata |
| `GET` | `/auth/passkeys` | bearer | List passkey metadata |
| `DELETE` | `/auth/passkeys/{credential_id}` | bearer | Remove one passkey |
| `POST` | `/auth/recovery/verify` | – | Exchange the recovery code for a session + new code |
| `POST` | `/auth/recovery/rotate` | bearer | Replace the recovery code |
| `POST` | `/auth/logout` | bearer | Delete the current session |
| `GET` | `/me` | bearer | Return the account id |
| `GET` | `/me/settings` | bearer | Return `{data, version, updated_at}` |
| `PUT` | `/me/settings` | bearer | Write `{data, version?}`; `409` on a stale version |
| `DELETE` | `/me` | bearer | Delete the account and all account data |

Invalid challenges, credentials, signatures, and recovery codes all return a generic `400` message.
The API never reveals whether an account or credential exists. Options and recovery codes expire or
become unusable after one attempt.

Settings are validated and bounded: at most 5,000 IDs per series list (`selected` and `known` are
limited separately), 20,000 seen-episode IDs, and 100 platform preferences, each ID between 1 and
100 characters. Unknown nested fields are rejected (`422`).

## Data model

Migration `20261116_08_add_auth_tables` introduced accounts and sessions; migration
`20261121_09_replace_email_auth_with_passkeys` removes the email login artifacts and adds the
passkey tables.

- `users` — `id` (UUID) and `created_at` only. No email, name, or profile data.
- `passkey_credentials` — raw `credential_id`, `public_key`, `sign_count`, `created_at`,
  `last_used_at`, and the owning user UUID.
- `auth_challenges` — base64url challenge, kind, optional user UUID for pending registrations,
  `created_at`, `expires_at`. Deleted when expired or consumed.
- `recovery_codes` — a 12-character lookup prefix, salted scrypt hash, salt, `created_at`, and user
  UUID. The plaintext exists only in the response that creates it.
- `user_sessions` — SHA-256 hash of the session token as the primary key, user UUID, and timestamps.
  No IP address or user agent is stored.
- `user_settings` — one row per user: JSON document, monotonically increasing `version`,
  `updated_at`.

The earlier email migration is part of history and must not be edited; the new migration drops
`login_tokens`, the `users.email` / `email_verified_at` columns, and `user_sessions.user_agent`.
Existing accounts keep their UUID, sessions, and settings, but they have no passkey and need a
fresh registration (or the email-era session while it is still valid).

## Security decisions

- **No personal data.** No email address, name, username, IP address, or user agent. The credential
  ID and signature counter are technical values required by WebAuthn, not a user identity.
- **Discoverable credentials with user verification required.** Registration uses
  `residentKey: required`; both ceremonies set `userVerification: required`, so a PIN/biometric
  check is enforced by the authenticator.
- **Real cryptographic verification.** `py_webauthn` parses the attestation/assertion, checks the
  challenge, origin, RP ID, and signature, and supports ES256 and EdDSA. Attestation is not
  requested (`none`), so no device manufacturer identity is stored.
- **Single-use, short-lived challenges** (5 minutes) are stored server-side, removed on first use,
  and cleaned up after a day. A replayed or mismatched challenge is rejected.
- **Origin allowlist.** Only configured frontend origins can complete a ceremony; the frontend
  origin is expected even though the API lives on a different host.
- **Signature counters are advisory.** Synced passkeys can report lower counters on another device.
  The server stores the highest value seen instead of locking legitimate users out.
- **Recovery codes** are 32 random URL-safe characters, stored as a salted scrypt digest
  (`n=2^14`, `r=8`, `p=1`) with a short lookup prefix. They are single-use and rotated on every use.
- **Session tokens** are 32 random bytes; only their SHA-256 digest is stored. Sessions last 30 days.
  `last_seen_at` is refreshed at most once per hour to avoid a write on every request.
- **Account deletion** removes credentials, recovery codes, challenges, sessions, and settings.

This is intentionally simple user management: no roles, no admin UI, no profile, no email change
flow, and no external identity provider.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTH_RP_ID` | `localhost` | Frontend hostname used as the WebAuthn relying party id |
| `AUTH_RP_NAME` | `Episode Calendar` | Display name shown by authenticators |
| `AUTH_EXPECTED_ORIGINS` | localhost dev origins | Comma-separated frontend origins allowed for ceremonies |

Challenge TTL and session TTL live in `src/episode_calendar/api/config.py`. There are no API keys,
mail secrets, or per-message costs. Local development needs no external account.

## Cloud deployment

Terraform passes `AUTH_RP_ID`, `AUTH_RP_NAME`, and `AUTH_EXPECTED_ORIGINS` to Cloud Run. Set them in
`infra/terraform.tfvars` to the Firebase Hosting site, for example:

```hcl
auth_rp_id            = "episode-calendar-67234.web.app"
auth_rp_name          = "Episode Calendar"
auth_expected_origins = "https://episode-calendar-67234.web.app"
```

The old Brevo secret and its Secret Manager IAM binding are removed. Existing secret versions can be
deleted after the migration; keeping them does not affect the app. The complete first-deployment
sequence lives in the README.
