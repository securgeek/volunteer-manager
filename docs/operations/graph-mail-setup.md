# Sending portal mail through Microsoft Graph

The portal sends sign-in links as the conference's volunteer address (**`MAIL_SENDER`** in
`.env`, e.g. `volunteers@example.org`) using the Microsoft Graph
`sendMail` API over HTTPS. This works even though SMTP AUTH is disabled in the tenant.

The credential lives only in the **`mailrelay`** container, not in the portal. The portal
hands the relay a plain-text `{to, subject, body}`, and the relay always sends as
`MAIL_SENDER`. See [Mail relay](../services/mailrelay.md).

The relay uses **delegated** `Mail.Send`: it acts as a real account (`RELAY_MAILBOX`
in `.env`), which signs in once with a device code. It needs no admin consent and no
client secret. Here's how it works:

- The sender is usually a **Microsoft 365 group**, and Graph can't send from a group directly.
- So the relay sends from the relay mailbox with From set to `MAIL_SENDER`, and Exchange checks
  the relay mailbox's **Send As** right on the group.
- The relay can send as the relay mailbox and as anything the relay mailbox has Send As on. It can't read mail.

---

## 0. Before you start

- **Check that the relay mailbox has Send As on `MAIL_SENDER`.** Being a member isn't enough. Check from
  Exchange Online PowerShell (see "Running Exchange PowerShell" at the end):
  ```powershell
  $RELAY_MAILBOX = '<RELAY_MAILBOX from .env>'
  $MAIL_SENDER   = '<MAIL_SENDER from .env>'
  # Expect AccessRights {SendAs}. The message then shows the sender address only.
  Get-RecipientPermission $MAIL_SENDER | Where-Object Trustee -eq $RELAY_MAILBOX
  # Send on Behalf only: still sends, but shows "<name> on behalf of <group>".
  Get-UnifiedGroup $MAIL_SENDER | Format-List GrantSendOnBehalfTo
  ```
  To grant Send As: Exchange admin center → *Groups* → the group → *Settings* → *Send As*,
  or `Add-RecipientPermission $MAIL_SENDER -Trustee $RELAY_MAILBOX -AccessRights SendAs`.
- **Check that DKIM is on for the sender's domain.** In the Defender portal
  (security.microsoft.com), go to *Email & collaboration → Policies & rules → Threat
  policies → Email authentication settings → DKIM*. The domain should show
  **Enabled**. If it doesn't, follow the prompts to publish the two CNAME records and then
  enable it. DKIM does more than anything else here to keep sign-in links out of spam.

## 1. Set up the app registration (Entra admin center)

As the app's owner you can do all of this without an admin.

1. Go to entra.microsoft.com and open *Identity → Applications → App registrations*. Open
   **BSides Volunteer Portal Mailer**, or create it with *New registration*:
   - Supported account types: **this organizational directory only**
   - Redirect URI: leave it blank
2. On the **Overview** page, copy these two values:
   - **Application (client) ID**, which goes in `GRAPH_CLIENT_ID`
   - **Directory (tenant) ID**, which goes in `GRAPH_TENANT_ID`
3. Go to *Authentication → Advanced settings* and set **Allow public client flows** to
   **Yes**, then click **Save**. Device-code sign-in needs this setting.
4. Go to *API permissions*. Add **Microsoft Graph → Delegated permissions** for:
   - `Mail.Send`
   - `offline_access`
   - `openid`
   - `profile`

   Remove the **Application** `Mail.Send` permission if it's still listed. Nobody approved
   it, and if someone approves it later, it would let the app send as everyone in the
   tenant.
5. Go to *Certificates & secrets* and **delete any client secrets**. The relay doesn't use
   them, so there's no reason to keep a credential around.

## 2. Configure the portal and the relay

Add these to the deployment `.env`. Remove `GRAPH_CLIENT_SECRET` if it's still there.

```bash
MAIL_BACKEND=relay
MAIL_RELAY_TOKEN=<output of: openssl rand -hex 32>   # shared by portal and relay
GRAPH_TENANT_ID=<Directory (tenant) ID>              # relay only
GRAPH_CLIENT_ID=<Application (client) ID>            # relay only
RELAY_MAILBOX=<organizer>@example.org               # relay only: the account that signs in
MAIL_SENDER=volunteers@example.org                   # relay and vol_mail: the From address
```

The From address is always `MAIL_SENDER`. It's set here, once per deployment, never by a
request, and the relay won't start without it. The
relay needs outbound HTTPS access to `login.microsoftonline.com` and `graph.microsoft.com`.
The portal needs neither.

## 3. Start the relay and sign in

```bash
podman-compose up -d --build mailrelay
podman-compose logs mailrelay   # "ready ... through <RELAY_MAILBOX>" and "not signed in" (expected)
podman-compose exec mailrelay python relay.py login
```

`login` prints a code and a URL (microsoft.com/devicelogin):

1. Open the URL in any browser and enter the code.
2. Sign in as the **`RELAY_MAILBOX`** account and approve the permissions.
3. The command finishes with `Signed in as <RELAY_MAILBOX>`.

If you sign in as a different account, it refuses to save anything.

The sign-in is saved on the `mailrelay_state` volume, and the running relay picks it up
without a restart. To check:

```bash
podman-compose exec mailrelay python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:5002/health').read())"
# b'{"ok": true, "signed_in": true}'
```

## 4. Test it

Restart the stack so the portal picks up `MAIL_BACKEND=relay` and joins the
`mail_internal` network (see [Deployment](deployment.md#shipping-a-change)). Then send one
message through the whole path, portal to relay to Graph:

```bash
./maintenance.sh on; podman-compose build; ./maintenance.sh off
podman-compose exec portal python mailer.py --test you@example.com
```

If it succeeds, it prints `sent via relay: <id>`, and `podman-compose logs mailrelay` shows
`send <id> ok`. Check three things:

- The message arrived and is not in spam. Gmail and Outlook.com are both worth trying.
- **From** shows only the sender address, not "<name> on behalf of …" (see step 0), and the
  headers show `spf=pass`, `dkim=pass` and `dmarc=pass`.
- A copy appears in **the relay mailbox's** Sent Items.

Then try the real flow. Go to `/apply/`, enter the address of a volunteer who is already
registered, and click the link you receive.

Last, check that **only** the portal can reach the relay. Run this from the backend
container. It should fail with a name-resolution or connection error:

```bash
podman-compose exec backend python3 -c "import urllib.request; urllib.request.urlopen('http://mailrelay:5002/health', timeout=5)"
```

## Keeping it signed in

The sign-in stops working if the relay mailbox's password changes, if a Conditional Access policy asks
for sign-in again, if the account is disabled, or if the relay goes **90 days without
sending** (likely between conferences). Sends then fail with `sign-in expired`, and the
fix is to run `login` again:

```bash
podman-compose exec mailrelay python relay.py login
```

Nothing tells you automatically, so **run the `--test` command before each event**.

To revoke the sign-in on purpose, go to myaccount.microsoft.com → *Apps & services*, or
delete the volume with `podman volume rm <project>_mailrelay_state` after stopping the
relay.

### Common failures

| Error | Usual cause |
|---|---|
| `mail relay unreachable` | The relay isn't running (see its logs; it exits if a setting is missing), or the portal wasn't recreated after `mail_internal` was added |
| `mail relay returned 401` | `MAIL_RELAY_TOKEN` differs between the two containers; recreate both after changing it |
| `mail relay returned 503: not signed in` | Run `relay.py login` (step 3) |
| `mail relay returned 503: sign-in expired` | See "Keeping it signed in" |
| `login`: `AADSTS7000218` ... `client_assertion` or `client_secret` | **Allow public client flows** isn't on (step 1.3) |
| `login`: `AADSTS65001` consent / `AADSTS90094` admin approval | The tenant doesn't let users consent to these delegated permissions; an admin has to approve them |
| `login`: `AADSTS50199` / `AADSTS53003` | Conditional Access blocks device-code sign-in |
| `login`: `signed in as X, but RELAY_MAILBOX is ...` | You signed in with the wrong account; run it again |
| `login`: `no refresh token returned` | `offline_access` is missing from the delegated permissions |
| `AADSTS700016` application not found | Wrong tenant ID or client ID |
| `ErrorSendAsDenied` | the relay mailbox doesn't have Send As on `MAIL_SENDER` (see step 0) |
| `ErrorAccessDenied` / 403 | The delegated `Mail.Send` permission wasn't approved during `login`; run it again and accept |

## If better access comes later

With an admin's help, an **app-only** setup removes the dependence on the relay mailbox's sign-in: either
Exchange RBAC for Applications scoped to one mailbox, or tenant-wide `Mail.Send` with admin
consent. Switching means changing the relay's token code back to the client-credentials
flow. The input checks, network layout and portal stay the same.

## Running Exchange PowerShell

Use any of these. Commands that change permissions need the Exchange Administrator role.

- **Azure Cloud Shell**, if your tenant has it: shell.azure.com → PowerShell.
- **A container:** `podman run -it --rm mcr.microsoft.com/powershell`, then
  `Install-Module ExchangeOnlineManagement -Scope CurrentUser -Force`.
- **PowerShell installed locally:** on Fedora, `sudo dnf install powershell` after adding
  Microsoft's RHEL package repository.

Then connect. In a container, or anywhere else without a browser, add `-Device`; it prints
a code to enter at microsoft.com/devicelogin:

```powershell
$RELAY_MAILBOX = '<RELAY_MAILBOX from .env>'
Connect-ExchangeOnline -UserPrincipalName $RELAY_MAILBOX -Device
```
