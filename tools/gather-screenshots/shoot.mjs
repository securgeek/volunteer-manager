// Seeds a fresh demo stack with fake data and takes the README screenshots.
//
// Run by gather-screenshots.sh, which builds the stack and passes these flags:
//   node shoot.mjs --admin URL --public URL --portal-container NAME --out DIR [--only a,b]
//
// Phases run in order, each on the state the last one left: wizard (setup and schedule
// builder), portal (email key, signup open), signup (one volunteer in the browser, the rest
// through the API), approve, assign, volunteer (sign-in, My info, editing). --only runs a
// subset against a stack already part-way through, for working on one phase. Each phase
// expects the state the one before it leaves; running one twice adds its data twice.
import { execFileSync } from 'node:child_process'
import { generateKeyPairSync } from 'node:crypto'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import puppeteer from 'puppeteer-core'

import {
  BROWSER_VOLUNTEER, CATEGORIES, CONFERENCE, DAYS, LEAVE_PENDING, ORGANIZER, TIMES, VOLUNTEERS,
} from './data.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const PHASES = ['wizard', 'portal', 'signup', 'approve', 'assign', 'volunteer']

// ── arguments ────────────────────────────────────────────────────────────────
const args = {}
for (let i = 2; i < process.argv.length; i += 2) {
  args[process.argv[i].replace(/^--/, '')] = process.argv[i + 1]
}
for (const k of ['admin', 'public', 'portal-container', 'out']) {
  if (!args[k]) { console.error(`missing --${k}`); process.exit(2) }
}
const ADMIN = args.admin.replace(/\/$/, '')
const PUBLIC = args.public.replace(/\/$/, '')
const OUT = args.out
const ONLY = args.only ? args.only.split(',') : PHASES
mkdirSync(OUT, { recursive: true })

const CHROME = process.env.CHROME || [
  '/usr/bin/chromium-browser', '/usr/bin/chromium', '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
].find(existsSync)
if (!CHROME) { console.error('no Chromium or Chrome found; set CHROME=/path/to/it'); process.exit(1) }

const log = (...m) => console.log('•', ...m)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ── HTTP, with a cookie jar per client ───────────────────────────────────────
class Client {
  constructor(base) { this.base = base; this.jar = new Map() }

  async req(method, path, fields) {
    const headers = {}
    if (this.jar.size) headers.cookie = [...this.jar].map(([k, v]) => `${k}=${v}`).join('; ')
    let body
    if (fields) {
      body = new FormData()
      for (const [k, v] of Object.entries(fields)) {
        for (const one of Array.isArray(v) ? v : [v]) body.append(k, one)
      }
    }
    const resp = await fetch(this.base + path, { method, headers, body, redirect: 'manual' })
    for (const c of resp.headers.getSetCookie()) {
      const [pair] = c.split(';')
      const at = pair.indexOf('=')
      this.jar.set(pair.slice(0, at).trim(), pair.slice(at + 1).trim())
    }
    const text = await resp.text()
    let data = text
    try { data = JSON.parse(text) } catch { /* not JSON */ }
    if (!resp.ok) throw new Error(`${method} ${path} -> ${resp.status}: ${text.slice(0, 300)}`)
    return data
  }

  get(path) { return this.req('GET', path) }
  post(path, fields = {}) { return this.req('POST', path, fields) }
}

async function organizer_api() {
  const c = new Client(ADMIN)
  await c.post('/api/login', ORGANIZER)
  return c
}

async function until(what, fn, { tries = 60, every = 1000 } = {}) {
  for (let i = 0; i < tries; i++) {
    const v = await fn()
    if (v) return v
    await sleep(every)
  }
  throw new Error(`timed out waiting for ${what}`)
}

// ── the portal's mail, read from inside its container (MAIL_BACKEND=file) ─────
// The files belong to the container's user, so the host can't read them directly.
const READ_LINK = `
import email, email.policy, glob, os, re, sys
files = sorted(glob.glob('/app/logs/portal_mail/*.eml'), key=os.path.getmtime, reverse=True)
for f in files:
    with open(f, 'rb') as fh:
        m = email.message_from_binary_file(fh, policy=email.policy.default)
    if m['To'] == sys.argv[1]:
        found = re.search(r'[?&]t=([A-Za-z0-9_-]+)', m.get_body(('plain',)).get_content())
        if found:
            print(found.group(1))
            break
`
function newest_token(addr) {
  return execFileSync('podman', ['exec', args['portal-container'], 'python3', '-c', READ_LINK, addr],
    { encoding: 'utf8' }).trim()
}
async function wait_for_token(addr, not = '') {
  return until(`a sign-in link for ${addr}`, () => {
    const t = newest_token(addr)
    return t && t !== not ? t : null
  })
}

// ── browser helpers ──────────────────────────────────────────────────────────
let browser
// The organizer app is shot on a laptop-sized screen; the volunteer pages on a phone, which is
// how most volunteers open them.
const LAPTOP = { width: 1366, height: 860, deviceScaleFactor: 1 }
const PHONE = { width: 390, height: 844, deviceScaleFactor: 1, isMobile: true, hasTouch: true }

async function new_page({ dark = false, phone = false } = {}) {
  const ctx = await browser.createBrowserContext()
  const page = await ctx.newPage()
  await page.setViewport(phone ? PHONE : LAPTOP)
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: dark ? 'dark' : 'light' }])
  page.setDefaultTimeout(15000)
  return page
}

// Clicks the visible button (or link, or tab) whose text is exactly `label`. Steppers and
// dialogs keep hidden copies of the same buttons, so a plain text match can pick a hidden one.
async function click_text(page, label, within = 'button, a, [role=tab], [role=menuitem]') {
  const handle = await page.waitForFunction((label, within) => {
    const shown = [...document.querySelectorAll(within)].filter((el) => {
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0 && !el.disabled
    })
    const t = (el) => el.innerText.replace(/\s+/g, ' ').trim()
    // Exact first; then the label plus a badge or count ("Volunteers 13").
    return shown.find((el) => t(el) === label) ||
      shown.find((el) => t(el).startsWith(label + ' ') && /^\d+$/.test(t(el).slice(label.length + 1))) ||
      null
  }, {}, label, within)
  await handle.asElement().click()
}

async function fill(page, selector, value) {
  await page.locator(selector).fill(value)
}

// Sets a native input the way typing would, for time and number inputs.
async function set_native(page, handle, value) {
  await page.evaluate((el, v) => {
    el.value = v
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new Event('change', { bubbles: true }))
  }, handle, value)
}

async function settle(page, ms = 600) {
  await page.waitForNetworkIdle({ idleTime: 300, timeout: 10000 }).catch(() => {})
  await sleep(ms)
}

let shot_no = 0
async function shot(page, name, { full = false, element = null } = {}) {
  await settle(page)
  const path = join(OUT, `${name}.png`)
  if (element) {
    const el = typeof element === 'string' ? await page.waitForSelector(element) : element
    await el.screenshot({ path })
  } else {
    await page.screenshot({ path, fullPage: full })
  }
  shot_no++
  log(`saved ${name}.png`)
}

// ── phase: setup wizard and schedule builder ─────────────────────────────────
async function wizard() {
  const status = await new Client(ADMIN).get('/api/setup_status')
  const page = await new_page({ dark: true })
  await page.goto(`${ADMIN}/admin`)

  // Each step is skipped when an earlier run already did it, so --only wizard can resume.
  if (!status.has_users) {
    log('create the first organizer')
    await fill(page, 'input[placeholder="Username"]', ORGANIZER.username)
    await fill(page, 'input[placeholder="Password"]', ORGANIZER.password)
    await fill(page, 'input[placeholder="Confirm password"]', ORGANIZER.password)
    await click_text(page, 'Create Admin Account')
  }

  log('sign in')
  await page.waitForSelector('::-p-text(Sign In)')
  await fill(page, 'input[placeholder="Username"]', ORGANIZER.username)
  await fill(page, 'input[placeholder="Password"]', ORGANIZER.password)
  await click_text(page, 'Sign In')

  if (!status.has_title) {
    log('conference details')
    await page.waitForSelector('#conf-title')
    await fill(page, '#conf-title', CONFERENCE.title)
    await fill(page, '#conf-email', CONFERENCE.contact_email)
    await fill(page, '#conf-url', CONFERENCE.conference_url)
    await fill(page, '#conf-coc', CONFERENCE.code_of_conduct_url)
    await shot(page, 'setup-conference')
    await click_text(page, 'Save & Continue')

    log('theme logo')
    const file = await page.waitForSelector('input[type=file]')
    await file.uploadFile(join(HERE, 'logo.svg'))
    await page.waitForSelector('::-p-text(Change Image)')
    await click_text(page, 'Save & Continue')
  }
  if (status.has_schedule) { await page.browserContext().close(); return }

  log('schedule builder')
  await page.waitForSelector('button.method')
  await page.click('button.method')   // the first card: build it here
  await page.waitForSelector('::-p-text(Build Schedule)')

  for (const d of DAYS) {
    // Focusing the field pre-fills the day after the last one; take that when it's the day
    // wanted, else type the date. Then click away to close the calendar before adding.
    const input = await page.waitForSelector('.p-datepicker input')
    await input.click()
    await sleep(200)
    if (await page.evaluate((el) => el.value, input) !== d) {
      await page.keyboard.down('Control'); await page.keyboard.press('KeyA'); await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await input.type(d, { delay: 30 })
    }
    await page.click('.build-head h2')
    const typed = await page.evaluate((el) => el.value, input)
    if (typed !== d) throw new Error(`the date picker shows ${typed}, not ${d}`)
    await click_text(page, 'Add Day')
    await sleep(300)
  }
  const added = await page.$$eval('.p-steppanel:not([style*="none"]) .se-table tbody tr td:nth-child(3)',
    (tds) => tds.map((t) => t.textContent.trim()).filter(Boolean))
  log('days:', added.join(', '))
  await click_text(page, 'Next')

  for (const c of CATEGORIES) {
    await fill(page, 'input[placeholder^="Category name"]', c.name)
    await click_text(page, 'Add Category')
    await sleep(200)
    if (c.names) {
      const row = await page.waitForSelector(`xpath///tr[td[normalize-space()="${c.name}"]]`)
      const named = await row.$('::-p-text(Named)')
      await named.click()
      const area = await page.waitForSelector('textarea')
      await area.click({ clickCount: 3 })
      await area.type(c.names.join('\n'))
    }
  }
  await shot(page, 'setup-categories', { full: true })
  await click_text(page, 'Next')

  for (const [start, end, on] of TIMES) {
    const [s, e] = await page.$$('input.time-input')
    await set_native(page, s, start)
    await set_native(page, e, end)
    // Tick exactly the days this time runs on.
    const boxes = await page.$$('.add-opts .opt input')
    for (let i = 0; i < DAYS.length; i++) {
      const box = boxes[i]
      const checked = await page.evaluate((el) => el.checked, box)
      if (checked !== on.includes(i)) await box.click()
    }
    await click_text(page, 'Add Time')
    await sleep(300)
  }
  await shot(page, 'setup-times', { full: true })
  await click_text(page, 'Next')

  const fills = await page.$$('input.grid-input.fill')
  for (let i = 0; i < CATEGORIES.length; i++) {
    await set_native(page, fills[i], String(CATEGORIES[i].count))
  }
  // Busier mornings at the desk: one more on each 08:00 cell.
  const reg = await page.$$('tbody tr:first-child input.grid-input:not(.fill)')
  const heads = await page.$$eval('th.time-col', (ths) => ths.map((t) => t.textContent.trim()))
  for (let i = 0; i < heads.length; i++) {
    if (heads[i].startsWith('0800')) await set_native(page, reg[i], String(CATEGORIES[0].count + 1))
  }
  await shot(page, 'setup-positions', { full: true })
  await click_text(page, 'Create Schedule')

  log('skip the volunteer CSV import: they sign up through the portal')
  await page.waitForSelector('::-p-text(Import Volunteers)')
  await click_text(page, 'Skip for now')
  await page.waitForSelector('.ts-chip')
  await page.browserContext().close()
}

// ── phase: email key and signup ──────────────────────────────────────────────
async function portal() {
  const api = await organizer_api()
  log('upload a throwaway email key pair and open signup')
  const { publicKey } = generateKeyPairSync('rsa', {
    modulusLength: 2048,
    publicKeyEncoding: { type: 'spki', format: 'pem' },
    privateKeyEncoding: { type: 'pkcs8', format: 'pem' },
  })
  await api.post('/api/email_pubkey', { pubkey: publicKey })
  await api.post('/api/admin/portal/settings', { signup_open: '1' })
  await api.post('/api/admin/portal/sync_now')
  const pub = new Client(PUBLIC)
  await until('the portal to open signup', async () => {
    await api.post('/api/admin/portal/sync_now').catch(() => {})
    return (await pub.get('/api/portal/signup/status')).state === 'open'
  })
}

// ── phase: signups ───────────────────────────────────────────────────────────
// Answers for whatever the questionnaire asks, so an edited one still works.
function answers_for(fields, i) {
  const out = {}
  for (const f of fields) {
    if (f.type === 'ack') out[f.key] = true
    else if (f.type === 'check') out[f.key] = i % 2 === 0
    else if (f.type === 'choice' || f.type === 'select') {
      const plain = f.options.filter((o) => !o.detail)
      out[f.key] = plain[i % plain.length].value
    } else if (f.type === 'text' && f.required) out[f.key] = 'None'
  }
  return out
}

// Two shifts that don't overlap, rotating through what's on offer.
function prefs_for(timeslots, i) {
  const open = timeslots.filter((t) => !t.full)
  const first = open[i % open.length]
  const second = open.find((t, j) => j > open.indexOf(first) % open.length &&
    t.key !== first.key && !first.overlaps.includes(t.key)) ||
    open.find((t) => t.key !== first.key && !first.overlaps.includes(t.key))
  return [first.key, second.key]
}

async function signup_via_api([first, last, handle], i) {
  const addr = `${first}.${last}@example.org`.toLowerCase()
  const c = new Client(PUBLIC)
  await c.post('/api/portal/signup/check_email', { email: addr })
  await c.post('/api/portal/verify', { t: await wait_for_token(addr) })
  const { fields } = await c.get('/api/portal/signup/fields')
  const { timeslots } = await c.get('/api/portal/timeslots')
  const prefs = prefs_for(timeslots, i)
  await c.post('/api/portal/signup/submit', {
    signup: JSON.stringify({
      first_name: first, last_name: last, handle,
      answers: answers_for(fields, i),
      prefs, extras: i % 3 === 0 ? [timeslots.find((t) => !prefs.includes(t.key)).key] : [],
      add_any: i % 5 === 4,
    }),
  })
}

async function signup() {
  const v = BROWSER_VOLUNTEER
  const page = await new_page({ phone: true })

  log('the public pages')
  await page.goto(`${PUBLIC}/`)
  await shot(page, 'portal-landing')   // the first screen: a phone's full page is very long

  log('sign up in the browser')
  await page.goto(`${PUBLIC}/apply/`)
  await click_text(page, 'Get started')
  await fill(page, '#signup-email', v.email)
  await click_text(page, 'Continue')
  await page.waitForSelector('::-p-text(Check your email)')
  await shot(page, 'portal-check-email')

  const token = await wait_for_token(v.email)
  await page.goto(`${PUBLIC}/apply/verify?t=${token}`)
  await page.waitForSelector('::-p-text(Sign in)')
  await click_text(page, 'Sign in')

  await page.waitForSelector('#su-first')
  await fill(page, '#su-first', v.first)
  await fill(page, '#su-last', v.last)
  await fill(page, '#su-handle', v.handle)
  await shot(page, 'portal-signup-about')
  await click_text(page, 'Next')

  log('pick shifts and answer the questions')
  await page.waitForSelector('.slot')
  const slots = await page.$$('.slot')
  await slots[0].click()
  await sleep(200)
  // The second pick: the first row that isn't now greyed out as overlapping.
  for (const s of (await page.$$('.slot')).slice(1)) {
    const off = await page.evaluate((el) => el.classList.contains('off'), s)
    if (!off) { await s.click(); break }
  }
  await answer_visible(page)
  await shot(page, 'portal-signup-shifts', { full: true })
  await click_text(page, 'Next')

  await answer_visible(page)
  await shot(page, 'portal-signup-questions', { full: true })
  await click_text(page, 'Next')

  await page.waitForSelector('::-p-text(Submit signup)')
  await click_text(page, 'Submit signup')
  await page.waitForSelector("::-p-text(You're signed up!)")
  await page.browserContext().close()

  log(`sign up ${VOLUNTEERS.length} more through the API`)
  for (let i = 0; i < VOLUNTEERS.length; i++) await signup_via_api(VOLUNTEERS[i], i)

  const api = await organizer_api()
  const want = VOLUNTEERS.length + 1
  await until('the signups to reach the scheduler', async () => {
    await api.post('/api/admin/portal/sync_now').catch(() => {})
    const pending = await api.get('/api/admin/portal/pending')
    return pending.pending.length >= want
  })
}

// Ticks every acknowledgement and answers each unanswered choice on the current step.
async function answer_visible(page) {
  for (const box of await page.$$('.p-checkbox input[id^="su-ack"]')) {
    if (!(await page.evaluate((el) => el.checked, box))) await box.click()
  }
  const groups = await page.$$eval('input[type=radio][id^="su-"]', (els) =>
    [...new Set(els.map((e) => e.name || e.id.split('-').slice(0, 2).join('-')))])
  for (const g of groups) {
    const radios = await page.$$(`input[type=radio][id^="${g.startsWith('su-') ? g : 'su-' + g}"]`)
    const any = await Promise.all(radios.map((r) => page.evaluate((el) => el.checked, r)))
    if (!any.includes(true) && radios.length) {
      // The last option is the plain one in the default questionnaire (no detail box).
      await radios[radios.length - 1].click()
    }
  }
  for (const sel of await page.$$('.p-select')) {
    const empty = await page.evaluate((el) => el.classList.contains('p-select-empty') ||
      !!el.querySelector('.p-placeholder'), sel)
    if (!empty) continue
    await sel.click()
    const opt = await page.waitForSelector('.p-select-option:nth-child(3)')
    await opt.click()
  }
}

// ── phase: approval ──────────────────────────────────────────────────────────
async function approve() {
  log('approve most through the API, leaving a short queue for the screenshot')
  const api = await organizer_api()
  const rows = (await api.get('/api/admin/portal/pending')).pending
  // The browser volunteer is always approved, so their My info page shows scheduled shifts.
  const is_browser_vol = (v) => v.name.startsWith(`${BROWSER_VOLUNTEER.first} `)
  const keep = new Set(rows.filter((v) => !is_browser_vol(v)).slice(-(LEAVE_PENDING + 1))
    .map((v) => v.vol_id))
  let badge = 1002
  for (const v of rows) {
    if (!keep.has(v.vol_id)) {
      await api.post('/api/admin/portal/approve', { vol_id: v.vol_id, new_vol_id: String(badge++) })
    }
  }

  const page = await admin_page()
  await click_text(page, 'Volunteers')
  await page.waitForSelector('input[placeholder^="Badge"]')
  await fill(page, 'input[placeholder^="Badge"]', '1001')
  await shot(page, 'approve-pending')

  log('approve one in the page, with a badge number')
  await click_text(page, 'Approve')
  await settle(page)
  await page.browserContext().close()
}

async function admin_page() {
  const page = await new_page({ dark: true })
  await page.goto(`${ADMIN}/admin`)
  await page.waitForSelector('input[placeholder="Username"]')
  await fill(page, 'input[placeholder="Username"]', ORGANIZER.username)
  await fill(page, 'input[placeholder="Password"]', ORGANIZER.password)
  await click_text(page, 'Sign In')
  await page.waitForSelector('.ts-chip')
  return page
}

// ── phase: assignments ───────────────────────────────────────────────────────
async function assign() {
  const api = await organizer_api()
  log('assign volunteers to the shifts they picked')
  const vols = (await api.get('/api/admin/volunteers')).volunteers
  // The browser volunteer first, so they're always among those assigned.
  const active = vols.filter((v) => v.status === 'active')
    .sort((a, b) => Number(b.name.startsWith(`${BROWSER_VOLUNTEER.first} `)) -
                    Number(a.name.startsWith(`${BROWSER_VOLUNTEER.first} `)))
  const { schedule } = await api.get('/api/get_schedule')
  // "day_time" -> { category: [empty pos_key, …] }, so assignments spread across categories
  // instead of filling the first one listed.
  const free = new Map()
  for (const day of schedule) {
    for (const time of day.times) {
      const by_cat = {}
      for (const c of time.categories) by_cat[c.key] = c.positions.filter((p) => !p.vol_id).map((p) => p.key)
      free.set(`${day.key}_${time.key}`, by_cat)
    }
  }
  const take = (cell) => {
    const by_cat = free.get(cell)
    if (!by_cat) return null
    const most = Object.keys(by_cat).sort((a, b) => by_cat[b].length - by_cat[a].length)[0]
    return by_cat[most]?.shift() ?? null
  }
  // Everyone but the last few gets both picks, so the dialog screenshot still has people
  // who asked for an empty slot.
  for (const v of active.slice(0, -3)) {
    for (const p of (v.picks || []).filter((p) => p.kind === 'pref')) {
      const slot = take(`${p.day_key}_${p.time_key}`)
      if (slot) await api.post('/api/assign_vol', { pos_key: slot, vol_id: v.vol_id })
    }
  }

  const page = await admin_page()
  await click_text(page, '0800-1130', '.time-item')
  await shot(page, 'schedule')
  log('the slot dialog, picking a volunteer for an empty slot')
  const chip = await page.waitForFunction(() => [...document.querySelectorAll('.ts-chip')]
    .find((el) => el.querySelector('.empty-slot') && el.getBoundingClientRect().width > 0))
  await chip.asElement().click()
  await page.waitForSelector('.p-dialog')
  await page.click('.p-dialog .p-select')
  await page.waitForSelector('.p-select-overlay')
  await shot(page, 'assign-slot')
  await page.keyboard.press('Escape')
  await page.waitForSelector('.p-select-overlay', { hidden: true })
  await page.keyboard.press('Escape')
  await page.waitForSelector('.p-dialog-mask', { hidden: true })

  log('the volunteer list, now with shifts')
  await click_text(page, 'Volunteers')
  const table = await page.waitForSelector('.p-datatable', { visible: true })
  await page.evaluate((el) => window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 70), table)
  await shot(page, 'volunteer-list')
  await page.browserContext().close()
}

// ── phase: a returning volunteer signs in and edits ──────────────────────────
async function volunteer() {
  const v = BROWSER_VOLUNTEER
  const before = newest_token(v.email)
  const page = await new_page({ phone: true })
  await page.goto(`${PUBLIC}/apply/login`)
  await fill(page, '#apply-email', v.email)
  await shot(page, 'portal-sign-in')
  await click_text(page, 'Send me a link')
  await page.waitForSelector('::-p-text(Check your email)')

  const token = await wait_for_token(v.email, before)
  await page.goto(`${PUBLIC}/apply/verify?t=${token}`)
  await page.waitForSelector('#apply-remember')
  await shot(page, 'portal-confirm')
  await click_text(page, 'Sign in')

  await page.waitForSelector('::-p-text(Change shifts or details)')
  await shot(page, 'portal-my-info', { full: true })
  await click_text(page, 'Change shifts or details')
  await page.waitForSelector('::-p-text(Save changes)')
  await click_text(page, 'Saturday 0800-1130', '.extras label')
  await shot(page, 'portal-edit', { full: true })
  await page.browserContext().close()
}

// ── run ──────────────────────────────────────────────────────────────────────
const RUN = { wizard, portal, signup, approve, assign, volunteer }
browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--lang=en-US', '--disable-features=Translate'],
})
try {
  for (const p of PHASES.filter((p) => ONLY.includes(p))) {
    log(`── ${p}`)
    await RUN[p]()
  }
  log(`done: ${shot_no} screenshots in ${OUT}`)
} catch (e) {
  // Save what the browser shows, to see where it stopped.
  for (const pg of (await browser.pages())) {
    await pg.screenshot({ path: join(OUT, '_failed.png'), fullPage: true }).catch(() => {})
  }
  console.error(e)
  process.exitCode = 1
} finally {
  await browser.close()
}

