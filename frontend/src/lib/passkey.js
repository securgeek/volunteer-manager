// Passkey (WebAuthn) ceremonies for the organizer app; the server side is
// backend/routers/passkeys.py.
//
// @simplewebauthn/browser is imported lazily. Only the login screen and the My Passkeys dialog
// use it, and both are under /admin, so it stays out of the landing page's bundle.
//
// Like the rest of the app, requests are FormData; the credential travels as a JSON string in
// a `credential` field.

export function passkeys_supported() {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential
}

// Whether the server can do passkeys at all (PORTAL_PUBLIC_URL set, https or localhost).
export async function passkeys_enabled() {
  if (!passkeys_supported()) return false
  try {
    const resp = await fetch('/api/passkeys/config')
    return resp.ok && (await resp.json()).enabled === true
  } catch { return false }
}

async function post(url, fields = {}) {
  const fd = new FormData()
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  try {
    const resp = await fetch(url, { method: 'POST', body: fd })
    const body = await resp.json().catch(() => null)
    return { ok: resp.ok, status: resp.status, body }
  } catch {
    return { ok: false, status: 0, body: { detail: 'Unable to reach the server.' } }
  }
}

// A cancelled browser prompt is not an error worth showing; callers check `.cancelled`.
function ceremony_error(e) {
  const err = new Error(e?.message || 'The passkey prompt failed.')
  const code = e?.code || ''
  if (e?.name === 'NotAllowedError' || code === 'ERROR_CEREMONY_ABORTED') {
    err.cancelled = true
    err.message = 'Cancelled, or no passkey was chosen.'
  } else if (code === 'ERROR_AUTHENTICATOR_PREVIOUSLY_REGISTERED') {
    err.message = 'This device already has a passkey for your account.'
  } else if (code === 'ERROR_AUTHENTICATOR_NO_SUPPORTED_PUBKEYCREDPARAMS_ALG') {
    err.message = 'This device does not support any of the passkey algorithms this site accepts.'
  } else if (code === 'ERROR_INVALID_DOMAIN' || code === 'ERROR_INVALID_RP_ID') {
    err.message = 'Passkeys only work at the site’s main address.'
  }
  return err
}

// Sign in. Resolves to { username, role }, like POST /api/login.
export async function passkey_sign_in() {
  const { startAuthentication } = await import('@simplewebauthn/browser')
  const opts = await post('/api/passkeys/login/options')
  if (!opts.ok) throw new Error(opts.body?.detail || 'Passkeys are unavailable.')
  let cred
  try { cred = await startAuthentication({ optionsJSON: opts.body }) }
  catch (e) { throw ceremony_error(e) }
  const r = await post('/api/passkeys/login/verify', { credential: JSON.stringify(cred) })
  if (!r.ok) throw new Error(r.body?.detail || 'Passkey sign-in failed.')
  return r.body
}

// Add a passkey to the signed-in account. Resolves to the stored passkey's list entry.
// A blank name lets the server use the passkey manager's name ("Bitwarden", "Google
// Password Manager"), which says more than a guess from this browser could: with a
// phone over QR code, the passkey isn't on this device at all.
export async function passkey_register(password, name) {
  const { startRegistration } = await import('@simplewebauthn/browser')
  const opts = await post('/api/passkeys/register/options', { password })
  if (!opts.ok) throw new Error(opts.body?.detail || 'Could not start adding a passkey.')
  let cred
  try { cred = await startRegistration({ optionsJSON: opts.body }) }
  catch (e) { throw ceremony_error(e) }
  const r = await post('/api/passkeys/register/verify',
                       { credential: JSON.stringify(cred), name })
  if (!r.ok) throw new Error(r.body?.detail || 'The passkey was not accepted.')
  return r.body
}
