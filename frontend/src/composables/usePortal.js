import { ref } from 'vue'

// Volunteer-facing side of the site. Everything here talks to /api/portal/* only, which
// nginx routes to the portal service — never to the scheduler backend.

// contact_email, conference_url and code_of_conduct_url are the organizers' (App Settings);
// '' when unset, and the pages then leave that line out.
const branding = ref({ title: 'Volunteer Portal', has_logo: false, has_favicon: false,
                       has_header_logo: false, contact_email: '', conference_url: '',
                       code_of_conduct_url: '' })
let loaded = null

function apply_favicon(href) {
  let link = document.querySelector("link[rel~='icon']")
  if (!link) {
    link = document.createElement('link')
    link.rel = 'icon'
    document.head.appendChild(link)
  }
  link.href = href
}

// Branding is pushed to the portal by the scheduler. The logo is served as its own
// cacheable URL rather than inlined, so a phone fetches it once, not on every load.
export function usePortalBranding() {
  if (!loaded) {
    loaded = fetch('/api/portal/branding')
      .then(r => (r.ok ? r.json() : null))
      .then(data => {
        if (!data) return
        branding.value = data
        document.title = data.title
        if (data.has_favicon) apply_favicon('/api/portal/branding/favicon')
      })
      .catch(() => { /* portal unreachable — keep the neutral shell */ })
  }
  // logo is the year's theme logo (the favicon is made from it); header_logo the smaller one
  // in the pages' upper left.
  return {
    branding,
    logo_url: '/api/portal/branding/logo',
    header_logo_url: '/api/portal/branding/header_logo',
  }
}

// FormData POST, same convention as the scheduler. Returns { ok, status, body } and
// never throws, so views can render a message for every outcome.
export async function portal_post(path, fields = {}) {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) form.append(k, v)
  try {
    const resp = await fetch(`/api/portal/${path}`, { method: 'POST', body: form })
    const body = await resp.json().catch(() => ({}))
    return { ok: resp.ok, status: resp.status, body }
  } catch {
    return { ok: false, status: 0, body: { detail: "Couldn't reach the server. Try again." } }
  }
}

export async function portal_get(path) {
  try {
    const resp = await fetch(`/api/portal/${path}`)
    const body = await resp.json().catch(() => ({}))
    return { ok: resp.ok, status: resp.status, body }
  } catch {
    return { ok: false, status: 0, body: { detail: "Couldn't reach the server. Try again." } }
  }
}

// The address typed on the login page, kept for the inbox page's resend button.
// sessionStorage rather than the URL, so the address stays out of history and logs.
export const APPLY_EMAIL_KEY = 'bsc_apply_email'
