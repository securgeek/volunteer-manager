import { ref } from 'vue'

// The volunteer portal's editable content (Volunteer Portal → Landing page / Questionnaire /
// Invite email): backend routers/portal_content.py. One copy shared by the three editors, so
// switching tabs doesn't refetch, and saving one part doesn't lose edits in another.

const data    = ref(null)     // GET /api/admin/portal/content
const loading = ref(false)
const error   = ref('')

async function post(path, fields) {
  const form = new FormData()
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) form.append(k, v)
  try {
    const resp = await fetch(path, { method: 'POST', body: form })
    const body = await resp.json().catch(() => ({}))
    return { ok: resp.ok, body }
  } catch {
    return { ok: false, body: { detail: 'Unable to reach the server.' } }
  }
}

export function usePortalContent() {
  async function load(force = false) {
    if (data.value && !force) return
    loading.value = true
    error.value = ''
    try {
      const resp = await fetch('/api/admin/portal/content')
      if (resp.ok) data.value = await resp.json()
      else error.value = (await resp.json().catch(() => ({}))).detail || 'Failed to load.'
    } catch {
      error.value = 'Unable to reach the server.'
    } finally {
      loading.value = false
    }
  }

  // The part as it stands: the saved copy, else the default. A deep copy, for editing.
  function current(part) {
    const d = data.value
    if (!d) return null
    return JSON.parse(JSON.stringify(d.saved[part] ?? d.defaults[part]))
  }

  // value null puts the part back to its default.
  async function save(part, value) {
    const r = await post('/api/admin/portal/content',
                         { part, value: value === null ? undefined : JSON.stringify(value) })
    if (r.ok && data.value) data.value.saved[part] = r.body.saved
    return r
  }

  function preview(part, value) {
    return post('/api/admin/portal/content/preview', { part, value: JSON.stringify(value) })
  }

  return { data, loading, error, load, current, save, preview }
}
