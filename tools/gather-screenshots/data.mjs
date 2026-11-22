// Fake data for the demo stack. Every name is invented and every address is @example.org.

export const ORGANIZER = { username: 'organizer', password: 'demo-password-1' }

export const CONFERENCE = {
  title: 'DemoCon 2027',
  contact_email: 'volunteers@example.org',
  conference_url: 'https://example.org/',
  code_of_conduct_url: 'https://example.org/code-of-conduct',
}

// Two event days, as typed into the builder's date picker (m/d/yyyy).
export const DAYS = ['5/14/2027', '5/15/2027']

// [start, end, which days run it]. Saturday's afternoon ends earlier, so the builder gives it
// its own key (1500-1700).
export const TIMES = [
  ['08:00', '11:30', [0, 1]],
  ['11:30', '15:00', [0, 1]],
  ['15:00', '19:00', [0]],
  ['15:00', '17:00', [1]],
]

// name, then how many at every time; a named category lists its positions.
export const CATEGORIES = [
  { name: 'Registration', count: 3 },
  { name: 'Security', count: 3 },
  { name: 'Room Monitor', count: 3, names: ['Track 1', 'Track 2', 'Workshop Room'] },
  { name: 'Floater', count: 2 },
]

// The volunteer who signs up through the browser, so the screenshots show each step.
export const BROWSER_VOLUNTEER = {
  first: 'Riley', last: 'Okafor', handle: 'packetpixie', email: 'riley.okafor@example.org',
}

// Signed up through the API. [first, last, handle]
export const VOLUNTEERS = [
  ['Avery', 'Lindqvist', 'nullbyte'],
  ['Jordan', 'Mbeki', ''],
  ['Casey', 'Tanaka', 'hexhound'],
  ['Morgan', 'Delacroix', ''],
  ['Quinn', 'Abernathy', 'rootkitten'],
  ['Sasha', 'Ivanova', ''],
  ['Rowan', 'Achebe', 'bitflip'],
  ['Emerson', 'Kowalski', ''],
  ['Parker', 'Nakamura', 'zeroday'],
  ['Dakota', 'Haddad', ''],
  ['Skyler', 'Fontaine', 'opcode'],
  ['Reese', 'Oyelaran', ''],
  ['Harper', 'Vasquez', 'sigsegv'],
  ['Finley', 'Okonkwo', ''],
  ['Blake', 'Sorensen', 'nopsled'],
  ['Hayden', 'Marchetti', ''],
  ['Kendall', 'Yamamoto', 'cronjob'],
  ['Logan', 'Adeyemi', ''],
  ['Peyton', 'Lindgren', 'portscan'],
  ['Remy', 'Castellanos', ''],
  ['Sawyer', 'Nwosu', 'stackcanary'],
  ['Tatum', 'Bergstrom', ''],
  ['Elliot', 'Ramaswamy', 'sudoer'],
  ['Marlowe', 'Fitzgerald', ''],
]

// Left pending, so the approval screenshot has someone to approve.
export const LEAVE_PENDING = 3
