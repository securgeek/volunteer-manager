// The browser's copy of the portal's input rules, against the cases the portal runs too.
//   node --test src/lib/portal_rules.test.js        (from frontend/; no packages needed)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { check, problem, normal, signup_answers } from './portal_rules.js'

const fixture = new URL('../../../portal/test_data/validation_cases.json', import.meta.url)
const { cases } = JSON.parse(readFileSync(fixture, 'utf8'))

for (const c of cases) {
  test(`${c.kind}: ${JSON.stringify(c.value).slice(0, 40)} -> ${c.expect}`, () => {
    const r = check(c.kind, c.value, { max: c.max || 0 })
    assert.equal(r.problem ?? 'ok', c.expect, `rule ${r.rule}`)
    if ('clean' in c) assert.equal(r.clean, c.clean)
    // The form shows words for anything it refuses, and nothing for what it accepts.
    assert.equal(problem(c.kind, c.value, { label: 'Field', max: c.max || 0 }) !== '',
                 c.expect !== 'ok')
  })
}

test('every kind and outcome is covered', () => {
  assert.deepEqual(new Set(cases.map(c => c.kind)),
                   new Set(['name', 'handle', 'email', 'text', 'pick_key', 'token']))
  assert.deepEqual(new Set(cases.map(c => c.expect)), new Set(['ok', 'error', 'anomaly']))
})

test('normal keeps lines in free text', () => {
  assert.equal(normal('  a   b \r\n  c  ', true), 'a b\nc')
  assert.equal(normal('  a   b  '), 'a b')
})

test('signup_answers sends only what is asked now, in its own shape', () => {
  const fields = [
    { key: 'tshirt', type: 'select', options: [{ value: 'M' }, { value: 'L' }] },
    { key: 'experience', type: 'choice',
      options: [{ value: 'none' }, { value: 'other_con', detail: { key: 'other_con_name', max: 80 } }] },
    { key: 'flexible', type: 'check' },
    { key: 'ack_rules', type: 'ack' },
    { key: 'dietary', type: 'text', max: 200 },
  ]
  const draft = {
    tshirt: 'XXXL',                                 // no longer offered
    experience: 'other_con', other_con_name: '  BSides   DE ',
    flexible: 'yes',                                // not a boolean
    ack_rules: true,
    dietary: 'none\r\n  thanks ',
    removed_question: 'still in the draft',
    other_detail: 'for an option not chosen',
  }
  assert.deepEqual(signup_answers(fields, draft), {
    experience: 'other_con', other_con_name: 'BSides DE', ack_rules: true,
    dietary: 'none\nthanks',
  })
})
