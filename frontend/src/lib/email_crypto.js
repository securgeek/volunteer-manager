// Decrypting volunteers' email addresses in the browser, with the organizers' private key.
//
// email_enc is RSA-OAEP with SHA-256 over the address, base64 (portal security.encrypt_email,
// AddVolunteerForm.vue). The private key is PKCS#8 PEM, as `openssl genpkey` writes it
// (frontend/CLAUDE.md → Email encryption). It never leaves this page: callers keep the
// CryptoKey in memory and drop it when done.
//
// Used by EmailKeyManager (one volunteer) and VolunteersTab (everyone at once).

export function pem_to_der(pem) {
  const b64 = pem.replace(/-----BEGIN [^-]+-----/, '').replace(/-----END [^-]+-----/, '')
                 .replace(/\s/g, '')
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer
}

// Throws if the PEM isn't an RSA PKCS#8 private key.
export function import_private_key(pem) {
  return crypto.subtle.importKey('pkcs8', pem_to_der(pem.trim()),
                                 { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt'])
}

// Throws if the key doesn't match (OperationError) or the ciphertext isn't base64.
export async function decrypt_email(key, email_enc) {
  const ct = Uint8Array.from(atob(email_enc), c => c.charCodeAt(0))
  return new TextDecoder().decode(await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, key, ct))
}

// Decrypt every { vol_id, email_enc } given. Returns { emails: {vol_id: address}, failed: n }.
// One bad ciphertext doesn't stop the rest; if none decrypt, the key is almost certainly wrong.
export async function decrypt_all(key, vols) {
  const emails = {}
  let failed = 0
  for (const v of vols) {
    if (!v.email_enc) continue
    try { emails[v.vol_id] = await decrypt_email(key, v.email_enc) }
    catch { failed++ }
  }
  return { emails, failed }
}
