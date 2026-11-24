// The volunteer portal's input rules, checked in the browser before anything is sent.
//
// The portal enforces the same rules (portal/validation.py), and anything that reaches it
// breaking one is recorded as an anomaly, a likely attack. So these must refuse at least
// everything the portal would: portal/test_data/validation_cases.json holds the cases, and
// both `node --test src/lib/portal_rules.test.js` and portal/test_validation.py run every one.
// Change a rule here and there together, and add the case.
//
// Lengths are counted in code points, as Python does (an emoji is 1, not 2). Invisible
// characters are written with String.fromCodePoint, never as literals.

export const NAME_MAX = 60
export const HANDLE_MAX = 40
export const EMAIL_MAX = 254
export const EMAIL_LOCAL_MAX = 64

const ZWJ = String.fromCodePoint(0x200d)        // joins emoji sequences; allowed
const NBSP = String.fromCodePoint(0xa0)
const RSQUO = String.fromCodePoint(0x2019)
const BAD = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}\p{Zl}\p{Zp}]/u
const NAME_OK = new RegExp(`^[\\p{L}\\p{M} '${RSQUO}.\\-]+$`, 'u')
const HANDLE_BANNED = /[<>{}`]/
const EMAIL = /^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$/
export const PICK_KEY = /^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$/
export const TOKEN = /^[A-Za-z0-9_-]{20,128}$/

const length = (s) => [...s].length

function bad_char(text, newlines) {
  for (const c of text) {
    if ((newlines && c === '\n') || c === ZWJ) continue
    if (BAD.test(c)) return true
  }
  return false
}

// NFC, \r\n -> \n, no-break spaces -> spaces, runs of spaces collapsed, trimmed (per line
// when newlines are kept). Never a problem: the portal does the same.
export function normal(text, newlines = false) {
  const t = text.normalize('NFC').replaceAll('\r\n', '\n').replaceAll(NBSP, ' ')
  if (newlines) return t.split('\n').map(l => l.replace(/ {2,}/g, ' ').trim()).join('\n').trim()
  return t.replace(/ {2,}/g, ' ').trim()
}

// Check one value. kind: name | handle | email | text | pick_key | token.
// Returns { clean, problem, rule }: problem is null, 'error' (an ordinary mistake, e.g. empty
// when required) or 'anomaly' (what the portal would record); rule names which.
export function check(kind, value, { max = 0, required = false } = {}) {
  if (typeof value !== 'string') return { clean: '', problem: 'anomaly', rule: 'wrong_type' }
  if (kind === 'pick_key' || kind === 'token') {
    const ok = (kind === 'pick_key' ? PICK_KEY : TOKEN).test(value)
    return { clean: value, problem: ok ? null : 'anomaly', rule: ok ? '' : kind }
  }
  const multiline = kind === 'text'
  if (bad_char(value.replaceAll('\r\n', '\n'), multiline))
    return { clean: '', problem: 'anomaly', rule: 'control_char' }
  const clean = normal(value, multiline)
  if (!clean) {
    const needed = required || kind === 'name' || kind === 'email'
    return { clean: '', problem: needed ? 'error' : null, rule: needed ? 'required' : '' }
  }
  const fail = (rule) => ({ clean, problem: 'anomaly', rule })
  if (kind === 'name') {
    if (length(clean) > NAME_MAX) return fail('too_long')
    return NAME_OK.test(clean) ? { clean, problem: null, rule: '' } : fail('name_chars')
  }
  if (kind === 'handle') {
    if (length(clean) > HANDLE_MAX) return fail('too_long')
    return HANDLE_BANNED.test(clean) ? fail('handle_chars') : { clean, problem: null, rule: '' }
  }
  if (kind === 'email') {
    const local = clean.slice(0, clean.lastIndexOf('@'))
    if (length(clean) > EMAIL_MAX || length(local) > EMAIL_LOCAL_MAX || !EMAIL.test(clean))
      return fail('email_syntax')
    return { clean, problem: null, rule: '' }
  }
  if (kind === 'text') {
    return length(clean) > max ? fail('too_long') : { clean, problem: null, rule: '' }
  }
  throw new Error(`unknown kind ${kind}`)
}

// The words for a problem, for the form. '' when there is none.
export function message(kind, result, label = '', max = 0) {
  if (!result.problem) return ''
  switch (result.rule) {
    case 'required':
      return kind === 'email' ? 'Enter your email address.' : `Enter your ${label.toLowerCase()}.`
    case 'control_char':
      return `${label || 'This'} can't contain invisible or control characters. Try typing it in.`
    case 'too_long':
      return `${label || 'This'}: at most ${kind === 'name' ? NAME_MAX : kind === 'handle' ? HANDLE_MAX : max} characters.`
    case 'name_chars':
      return `${label} can only use letters, spaces, apostrophes, hyphens and periods.`
    case 'handle_chars':
      return 'A handle can\'t use < > { } or `.'
    case 'email_syntax':
      return 'That doesn\'t look like an email address. It should look like name@example.com.'
    case 'token':
      return 'This link looks damaged. Copy the whole link from the email, or request a new one.'
    default:
      return `${label || 'This'} isn't valid.`
  }
}

// check + message in one call, for a form field: '' when fine.
export function problem(kind, value, { label = '', max = 0, required = false } = {}) {
  return message(kind, check(kind, value, { max, required }), label, max)
}

// The answers to send: only questions asked now, in the shapes the portal takes. A saved
// draft can hold answers to questions an organizer has since removed, a choice no longer
// offered, or a detail for an option not chosen; none of that is sent.
//   fields: the questionnaire (GET /api/portal/signup/fields); answers: the draft's.
export function signup_answers(fields, answers) {
  const out = {}
  for (const f of fields) {
    const v = answers[f.key]
    if (f.type === 'ack' || f.type === 'check') {
      if (typeof v === 'boolean') out[f.key] = v
    } else if (f.options) {
      const opt = f.options.find(o => o.value === v)
      if (!opt) continue
      out[f.key] = v
      const detail = opt.detail && answers[opt.detail.key]
      if (typeof detail === 'string') out[opt.detail.key] = normal(detail, true)
    } else if (typeof v === 'string' && v.trim()) {
      out[f.key] = normal(v, true)
    }
  }
  return out
}
