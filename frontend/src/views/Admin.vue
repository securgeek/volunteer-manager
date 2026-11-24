<script setup>
import { ref, computed, watch, onMounted, useTemplateRef, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { startEvents, stopEvents, useEvents } from '../composables/useEvents.js'
import Button    from 'primevue/button'
import Menu      from 'primevue/menu'
import Badge     from 'primevue/badge'
import Tabs      from 'primevue/tabs'
import TabList   from 'primevue/tablist'
import Tab       from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel  from 'primevue/tabpanel'

import SetupCreateAdmin      from '../components/setup/CreateAdmin.vue'
import SetupLogin            from '../components/setup/Login.vue'
import SetupSetTitle         from '../components/setup/SetTitle.vue'
import SetupSetLogo          from '../components/setup/SetLogo.vue'
import SetupScheduleSetup    from '../components/setup/ScheduleSetup.vue'
import SetupImportVolunteers from '../components/setup/ImportVolunteers.vue'

import ScheduleEditor  from '../components/ScheduleEditor.vue'
import AppSettings     from '../components/AppSettings.vue'
import EmailKeyManager from '../components/EmailKeyManager.vue'
import PortalStatus    from '../components/PortalStatus.vue'
import ShiftDemand     from '../components/ShiftDemand.vue'
import EmailTemplate   from '../components/EmailTemplate.vue'
import ShiftFlags      from '../components/ShiftFlags.vue'
import UserManager     from '../components/UserManager.vue'
import ApiTokens       from '../components/ApiTokens.vue'
import MyPasskeys      from '../components/MyPasskeys.vue'
import ProfileDialog   from '../components/ProfileDialog.vue'
import ScheduleView    from '../components/ScheduleView.vue'
import VolunteersTab   from './admin/VolunteersTab.vue'
import RadiosTab       from './admin/RadiosTab.vue'
import AuditTab        from './admin/AuditTab.vue'
import VolunteerCheckin from '../components/VolunteerCheckin.vue'

// ── state ─────────────────────────────────────────────────────────────────────
// 'loading' | 'create_admin' | 'login' | 'set_title' | 'set_logo' | 'import_schedule' | 'import_volunteers' | 'app'
const view      = ref('loading')
const app_title   = ref('Conference Scheduler')
const app_logo    = ref(null)
const app_favicon = ref(null)
const user_info = ref({ username: '', role: '' })

// ── boot ──────────────────────────────────────────────────────────────────────
function apply_favicon(data_url) {
  let link = document.querySelector("link[rel~='icon']")
  if (!link) {
    link = document.createElement('link')
    link.rel = 'icon'
    document.head.appendChild(link)
  }
  link.href = data_url || '/favicon.ico'
}

async function load_app_title() {
  try {
    const resp = await fetch('/api/app_config')
    if (resp.ok) {
      const data    = await resp.json()
      app_title.value   = data.title
      app_logo.value    = data.logo    || null
      app_favicon.value = data.favicon || null
      document.title    = data.title
      if (data.favicon) apply_favicon(data.favicon)
    }
  } catch { /* keep default */ }
}

async function check_status() {
  try {
    const resp = await fetch('/api/setup_status')
    const s    = await resp.json()
    if (!s.has_users)      { view.value = 'create_admin';      return }
    if (!s.authenticated)  { view.value = 'login';             return }
    user_info.value = { username: s.username, role: s.role }
    if (!s.has_title)      { view.value = 'set_title';         return }
    if (!s.has_schedule)   { view.value = 'import_schedule';   return }
    if (!s.has_volunteers) { view.value = 'import_volunteers'; return }
    view.value = 'app'
  } catch {
    view.value = 'login'
  }
}

onMounted(() => { load_app_title(); check_status() })

// ── wizard event handlers ─────────────────────────────────────────────────────
function on_logged_in(data) {
  user_info.value = { username: data.username, role: data.role }
  check_status()
}

function on_title_set(new_title) {
  app_title.value = new_title
  document.title  = new_title
  view.value      = 'set_logo'
}

function on_logo_set({ logo, favicon }) {
  app_logo.value    = logo    || null
  app_favicon.value = favicon || null
  if (favicon) apply_favicon(favicon)
  check_status()
}

// ── schedule ───────────────────────────────────────────────────────────────────
const schedule = ref([])

async function load_schedule() {
  try {
    const resp = await fetch('/api/get_schedule')
    if (resp.ok) schedule.value = (await resp.json()).schedule ?? []
  } catch { /* server unreachable — keep empty */ }
}

// ── SSE ────────────────────────────────────────────────────────────────────────
const { on } = useEvents()
on('schedule',   () => load_schedule())
on('assignment', () => { load_schedule(); shift_flags_ref.value?.reload() })
// A portal pull or a Make inactive / Reinstate: banners, the pending count, the volunteer
// list, and the schedule's needs-replacement markers can all change.
on('volunteers', () => {
  shift_flags_ref.value?.reload(); refresh_pending(); load_schedule(); volunteers_tab_ref.value?.reload()
})
on('checkin',    () => load_schedule())
on('note',       () => load_schedule())
on('rating',     () => load_schedule())
on('radio',      () => radios_tab_ref.value?.reload())

watch(view, (v) => {
  if (v === 'app') { load_schedule(); startEvents(); refresh_pending(); load_access() }
  else             { stopEvents() }
})

// ── audit log access ──────────────────────────────────────────────────────────
// The Audit Log tab is for organizers holding audit-log access (backend routers/audit_log.py),
// asked of the server, which checks it on every request too. null until known.
const route  = useRoute()
const router = useRouter()
const audit_access = ref(null)
async function load_access() {
  try {
    const resp = await fetch('/api/users/me')
    audit_access.value = resp.ok ? !!(await resp.json()).audit_access : false
  } catch { audit_access.value = false }
  if (audit_access.value && route.path === '/admin/audit') tab.value = 'audit'
  if (!audit_access.value && tab.value === 'audit') tab.value = 'schedule'
}

function on_schedule_updated(new_schedule) {
  schedule.value = new_schedule
}

// ── tabs ──────────────────────────────────────────────────────────────────────
// Schedule and Radios for everyone; Volunteers for organizers (answers, and email decrypt).
// The last tab is remembered per browser, falling back to Schedule if it isn't allowed.
const TAB_KEY = 'bsc_admin_tab'
const ADMIN_ROLES = ['organizer', 'owner']
const is_admin = computed(() => ADMIN_ROLES.includes(user_info.value.role))
// Check In Volunteer on the Schedule tab: the same roles that can check in from a slot.
const can_checkin = computed(() => [...ADMIN_ROLES, 'checkin'].includes(user_info.value.role))
// The scanner mounts only while open, so the camera runs only after the button is pressed.
const checkin_open = ref(false)

function saved_tab() {
  try { return localStorage.getItem(TAB_KEY) || 'schedule' } catch { return 'schedule' }
}
const tab = ref(saved_tab())
watch(tab, (t) => {
  try { localStorage.setItem(TAB_KEY, t) } catch { /* private mode */ }
  // The address follows the Audit Log tab, so it can be bookmarked and alert links land on it.
  if (t === 'audit' && route.path !== '/admin/audit') router.replace('/admin/audit')
  if (t !== 'audit' && route.path === '/admin/audit') router.replace('/admin')
})
// Only once the role is known: before the session loads it's '' and would reset every time.
watch(() => user_info.value.role, (role) => {
  if (role && !ADMIN_ROLES.includes(role) && tab.value === 'volunteers') tab.value = 'schedule'
}, { immediate: true })

const schedule_editor_ref = useTemplateRef('schedule_editor')
const shift_flags_ref     = useTemplateRef('shift_flags')
const schedule_view_ref   = useTemplateRef('schedule_view')
const volunteers_tab_ref  = useTemplateRef('volunteers_tab')
const radios_tab_ref      = useTemplateRef('radios_tab')

// The banner's Open: show the schedule, then the slot's dialog.
async function open_slot(key) {
  tab.value = 'schedule'
  await nextTick()
  schedule_view_ref.value?.open_slot_by_key(key)
}

// Portal signups awaiting approval: a badge on the Volunteers tab. Refreshed when the app
// loads, on the `volunteers` event, and after an approve or make-inactive.
const pending_count = ref(0)
async function refresh_pending() {
  if (!is_admin.value) return
  try {
    const resp = await fetch('/api/admin/portal/status')
    if (resp.ok) pending_count.value = (await resp.json()).pending_count || 0
  } catch { /* leave the last count */ }
}

// ── toolbar menus ─────────────────────────────────────────────────────────────
const app_settings_ref  = useTemplateRef('app_settings')
const email_key_ref     = useTemplateRef('email_key')
const portal_status_ref = useTemplateRef('portal_status')
const shift_demand_ref  = useTemplateRef('shift_demand')
const email_template_ref = useTemplateRef('email_template')
const user_manager_ref  = useTemplateRef('user_manager')
const api_tokens_ref    = useTemplateRef('api_tokens')
// Opened from Profile (user menu), which every role has: anyone can hold passkeys.
const my_passkeys_ref   = useTemplateRef('my_passkeys')
const profile_ref       = useTemplateRef('profile')
const cfv_menu_ref      = useTemplateRef('cfv_menu')
const admin_menu_ref    = useTemplateRef('admin_menu')
const user_menu_ref     = useTemplateRef('user_menu')

const cfv_menu_items = [
  { label: 'Volunteer Portal', icon: 'pi pi-sitemap',   command: () => portal_status_ref.value.open() },
  { label: 'Shift Demand',     icon: 'pi pi-chart-bar', command: () => shift_demand_ref.value.open() },
  { label: 'Schedule Email',   icon: 'pi pi-envelope',  command: () => email_template_ref.value.open() },
]

const admin_menu_items = [
  { label: 'Users',        icon: 'pi pi-users', command: () => user_manager_ref.value.open() },
  { label: 'App Settings', icon: 'pi pi-cog',   command: () => app_settings_ref.value.open({ title: app_title.value, logo: app_logo.value }) },
  { label: 'Email Key',    icon: 'pi pi-key',   command: () => email_key_ref.value.open() },
  { label: 'API Keys',     icon: 'pi pi-link',  command: () => api_tokens_ref.value.open() },
]

// Right-aligned, for every role. The group label is the header: who's signed in, as what.
const user_menu_items = computed(() => [{
  label: `${user_info.value.username} (${user_info.value.role})`,
  items: [
    { label: 'Profile',  icon: 'pi pi-user',     command: () => profile_ref.value.open() },
    { separator: true },
    { label: 'Sign Out', icon: 'pi pi-sign-out', command: () => logout() },
  ],
}])

function on_title_changed(new_title) {
  app_title.value = new_title
  document.title  = new_title
}

function on_logo_changed({ logo, favicon }) {
  app_logo.value    = logo    || null
  app_favicon.value = favicon || null
  apply_favicon(favicon)
}

// ── logout ─────────────────────────────────────────────────────────────────────
async function logout() {
  stopEvents()
  await fetch('/api/logout')
  localStorage.removeItem('bsc_username')
  localStorage.removeItem('bsc_role')
  user_info.value = { username: '', role: '' }
  audit_access.value = null
  view.value      = 'login'
}
</script>

<template>
  <!-- loading splash -->
  <div v-if="view === 'loading'"
       style="display:flex; justify-content:center; align-items:center; min-height:80vh;">
    <i class="pi pi-spin pi-spinner" style="font-size:2rem;" />
  </div>

  <SetupCreateAdmin      v-else-if="view === 'create_admin'"     :app-title="app_title"      @done="check_status" />
  <SetupLogin            v-else-if="view === 'login'"            :app-title="app_title" :app-logo="app_logo" @logged-in="on_logged_in" />
  <SetupSetTitle         v-else-if="view === 'set_title'"        :current-title="app_title"  @done="on_title_set" />
  <SetupSetLogo          v-else-if="view === 'set_logo'"         :setup="true"               @done="on_logo_set" @skipped="check_status" />
  <SetupScheduleSetup    v-else-if="view === 'import_schedule'"                              @done="check_status" @skipped="view = 'app'" />
  <SetupImportVolunteers v-else-if="view === 'import_volunteers'"                            @done="check_status" @skipped="view = 'app'" />

  <!-- ── main app ─────────────────────────────────────────────────────────── -->
  <div v-else-if="view === 'app'" style="padding:1rem;">
    <div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.75rem; flex-wrap:wrap;">
      <img v-if="app_logo" :src="app_logo" style="height:1.75rem; object-fit:contain;" />
      <strong style="font-size:1.1rem; margin-right:auto;">{{ app_title }}</strong>
      <template v-if="is_admin">
        <Button size="small" label="Call for Volunteers" icon="pi pi-chevron-down" iconPos="right"
                severity="secondary" @click="cfv_menu_ref.toggle($event)" />
        <Menu ref="cfv_menu" :model="cfv_menu_items" popup />
        <Button size="small" label="Admin" icon="pi pi-chevron-down" iconPos="right"
                severity="secondary" @click="admin_menu_ref.toggle($event)" />
        <Menu ref="admin_menu" :model="admin_menu_items" popup />
      </template>
      <Button size="small" :label="user_info.username" icon="pi pi-user" severity="secondary"
              @click="user_menu_ref.toggle($event)" />
      <Menu ref="user_menu" :model="user_menu_items" popup />
    </div>

    <Tabs v-model:value="tab">
      <TabList>
        <Tab value="schedule">Schedule</Tab>
        <Tab v-if="is_admin" value="volunteers">
          Volunteers <Badge v-if="pending_count" :value="pending_count" severity="warn" />
        </Tab>
        <Tab value="radios">Radios</Tab>
        <Tab v-if="audit_access" value="audit">Audit Log</Tab>
      </TabList>
      <TabPanels>
        <TabPanel value="schedule">
          <div v-if="is_admin || can_checkin" class="tab-top">
            <Button v-if="is_admin" size="small" label="Edit Schedule" icon="pi pi-calendar"
                    severity="secondary" @click="schedule_editor_ref.open()" />
            <Button v-if="can_checkin" size="small" label="Check In Volunteer" icon="pi pi-qrcode"
                    :severity="checkin_open ? undefined : 'secondary'"
                    @click="checkin_open = !checkin_open" />
          </div>
          <VolunteerCheckin v-if="checkin_open" @close="checkin_open = false" />
          <ShiftFlags v-if="is_admin" ref="shift_flags" @open-slot="open_slot" />
          <ScheduleView ref="schedule_view" :schedule="schedule" :role="user_info.role" :app-logo="app_logo"
                        @schedule-updated="on_schedule_updated" />
        </TabPanel>
        <TabPanel v-if="is_admin" value="volunteers">
          <VolunteersTab ref="volunteers_tab" @pending-changed="refresh_pending" />
        </TabPanel>
        <TabPanel value="radios">
          <RadiosTab ref="radios_tab" :role="user_info.role" />
        </TabPanel>
        <TabPanel v-if="audit_access" value="audit">
          <AuditTab />
        </TabPanel>
      </TabPanels>
    </Tabs>
  </div>

  <ScheduleEditor  ref="schedule_editor" />
  <AppSettings     ref="app_settings" @title-changed="on_title_changed" @logo-changed="on_logo_changed" />
  <EmailKeyManager ref="email_key" />
  <PortalStatus ref="portal_status" @open-email-key="email_key_ref.open()" />
  <ShiftDemand ref="shift_demand" />
  <EmailTemplate ref="email_template" />
  <UserManager     ref="user_manager" />
  <ApiTokens       ref="api_tokens" />
  <MyPasskeys      ref="my_passkeys" />
  <ProfileDialog   ref="profile" :username="user_info.username" :role="user_info.role"
                   @open-passkeys="my_passkeys_ref.open()" />
</template>

<style scoped>
.tab-top { margin-bottom: 0.75rem; display: flex; gap: 0.5rem; flex-wrap: wrap; }
</style>
