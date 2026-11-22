# Secrets and keys

This page lists each secret the system uses: where it lives, how to make it, and what
happens when it changes. For how the design limits what any one of them gives away, see
[Quality & security → Security in the design](../development/quality-and-security.md#security-in-the-design).

## Never commit or publish these

| File | What it holds |
|---|---|
| `.env` (and `.env.*`, `.env_*` backups) | Every secret on this page except the private key. |
| `private.pem` | The key that decrypts every volunteer's email address. |
| `api_key*` | Export API keys saved for `vol_mail`. |
| `box.json`, `vol_mail_state/` | Email ciphertext and check-in codes, from `vol_mail`. |
| Database dumps | Volunteer names and email ciphertext. |

`.gitignore` covers all of these, and `.rsync-filter` keeps `*.pem`, `*.key`, `api_key*` and
the `vol_mail` files off the server. `.env` is the exception: it's synced on purpose,
because the containers need it.

Before pushing, check what git would add:

```bash
git add -n . | grep -Ei '\.env|\.pem|\.key|api_key|box\.json|\.db$'   # expect only .env.example
gitleaks detect --no-git          # if you have gitleaks
```

## The volunteer email key pair

Volunteer addresses are never stored readable. The portal encrypts each one to the
organizers' **RSA public key** (RSA-OAEP with SHA-256) the moment it's submitted. Only
`private.pem` can decrypt them, and it never goes to the server:

- **Admin → Volunteers** decrypts in the organizer's browser, with the key chosen from disk.
  The server never sees a readable address.
- `vol_mail` decrypts on the organizer's machine to send schedule emails.

Make a pair (3072-bit RSA):

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out private.pem
openssl pkey -in private.pem -pubout -out public.pem
chmod 600 private.pem
```

Upload `public.pem` under **Admin → Email Key**. The scheduler pushes it to the portal on
the next sync, and signup can't open until the portal has one.

**Losing `private.pem` loses every address.** Keep it in a password manager or another
encrypted store, shared only with the organizers who send mail. Replacing the pair affects
only new signups: addresses already stored stay encrypted to the old key, and only the old
private key can read them.

## `EMAIL_HMAC_KEY`

`HMAC-SHA256(key, lowercased address)` is each volunteer's `email_hmac`. It's how the portal
finds a registered address and refuses a duplicate signup without storing the address. The
backend uses the same key to index volunteers added by hand.

- Make it with `openssl rand -hex 32`. The backend and the portal must have the same value.
- It's kept in the environment only, **never in a database**: `portal.db` holds the hashes,
  and keeping the key beside them would give away both halves.
- The portal refuses to start without it, rather than make a new one.
- **Changing it locks every volunteer out.** Their stored hashes stop matching, so nobody
  can sign in and duplicate detection stops working. Don't rotate it while volunteers exist.

## The sync secret

`SYNC_SECRET` signs every request from the scheduler to the portal (HMAC-SHA256 over the
method, path, timestamp, nonce and body hash). `SYNC_KEY_ID` names it.

To rotate it without dropping sync:

1. Generate a new secret with `openssl rand -hex 32`.
2. In `.env`, move the old value to `SYNC_SECRET_PREV` and put the new one in `SYNC_SECRET`.
3. Deploy. The portal accepts both while the backend switches to the new one.
4. Once sync is healthy (**Call for Volunteers → Volunteer Portal**), empty
   `SYNC_SECRET_PREV` and deploy again.

## `MAIL_RELAY_TOKEN`

The portal's bearer token for the mail relay. Make it with `openssl rand -hex 32`, at least
32 characters. To change it, update `.env` and restart the stack, so both containers get
the new value together. A mismatch shows up as `mail relay returned 401`.

## The relay's Microsoft sign-in

The relay holds a **delegated refresh token** for `RELAY_MAILBOX`, on the `mailrelay_state`
volume, mode 0600. It isn't in `.env` and isn't backed up. Microsoft issues a new one on
every use, and the relay saves each atomically.

- Create or renew it with `podman-compose exec mailrelay python relay.py login`.
- It stops working after a password change, a Conditional Access prompt, or 90 days without
  sending. See [Graph mail setup → Keeping it signed in](graph-mail-setup.md#keeping-it-signed-in).
- Revoke it at myaccount.microsoft.com → *Apps & services*, or delete the volume.

`vol_mail` uses the same app registration, but keeps its sign-in in memory only.

## Keys kept in the database

The backend generates these on first use and keeps them in `app_kv`:

| Key | What it does | Rotating it |
|---|---|---|
| `app_secret` | Signs the organizer session cookie. | Not exposed in the UI. Changing it signs every organizer out. |
| `checkin_key` | Makes each shift's check-in QR code. | **App Settings → Rotate key.** It voids every code already sent, so send the schedule emails again. |

## Export API keys

Made under **Admin → API Keys**, each with scopes, an expiry of up to 365 days and an
optional IP allow-list. The key is shown once, and only its SHA-256 is stored. Keys can't be
deleted, only revoked, so the audit log keeps saying which key did what. Save the key for
`vol_mail` in a file named `api_key*`, which git and rsync both ignore.
