<script setup>
import { ref, computed } from 'vue'
import Dialog  from 'primevue/dialog'
import Message from 'primevue/message'
import Tag     from 'primevue/tag'

// The organizer's honest view of signup, per timeslot (GET /api/admin/portal/demand).
// Volunteers see `remaining`, which includes the overage; this puts it beside the slots that
// actually exist and how many people picked each one.

const visible = ref(false)
const loading = ref(false)
const err     = ref('')
const data    = ref({ overage_pct: 0, timeslots: [] })

async function load() {
  loading.value = true
  err.value = ''
  try {
    const r = await fetch('/api/admin/portal/demand')
    if (r.ok) data.value = await r.json()
    else {
      const b = await r.json().catch(() => null)
      err.value = b?.detail || 'Failed to load shift demand.'
    }
  } catch { err.value = 'Unable to reach the server.' }
  finally { loading.value = false }
}

function open() { data.value = { overage_pct: 0, timeslots: [] }; load(); visible.value = true }
defineExpose({ open })

const days = computed(() => {
  const out = []
  for (const t of data.value.timeslots) {
    let d = out.find(x => x.day_key === t.day_key)
    if (!d) out.push(d = { day_key: t.day_key, day_str: t.day_str, rows: [] })
    d.rows.push(t)
  }
  return out
})

function ratio_severity(r) {
  if (r === null || r === undefined) return 'secondary'
  if (r > 1.0) return 'warn'
  return 'success'
}
</script>

<template>
  <Dialog v-model:visible="visible" header="Shift Demand" :style="{ width: '46rem' }" modal>
    <Message v-if="err" severity="error" :closable="false">{{ err }}</Message>

    <p class="hint">
      Signup offers each shift's slots plus <strong>{{ data.overage_pct }}%</strong>, to allow for
      volunteers who drop out before the event (set in Admin → Volunteer Portal). A ratio above
      1.0× means that overage is in use: if nobody drops out, not every pick can be honoured.
    </p>

    <table class="tbl">
      <thead>
        <tr>
          <th>Shift</th><th class="num">Slots</th><th class="num">Still open</th>
          <th class="num">Offered</th><th class="num">Picked by</th>
          <th class="num">Remaining</th><th>Ratio</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="d in days" :key="d.day_key">
          <tr class="day-row"><td colspan="7">{{ d.day_str }}</td></tr>
          <tr v-for="t in d.rows" :key="t.time_key">
            <td class="mono">{{ t.time_str }}</td>
            <td class="num mono">{{ t.slots }}</td>
            <td class="num mono">{{ t.open }}</td>
            <td class="num mono">{{ t.offered }}</td>
            <td class="num mono">{{ t.picks }}</td>
            <td class="num mono">{{ t.remaining }}</td>
            <td>
              <Tag v-if="t.ratio !== null" :severity="ratio_severity(t.ratio)" :value="t.ratio + '×'" />
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </template>
        <tr v-if="!days.length && !loading">
          <td colspan="7" class="muted">No timeslots in the schedule yet.</td>
        </tr>
      </tbody>
    </table>

    <p class="hint">
      <strong>Picked by</strong> counts pending and active volunteers who chose the shift as one of
      their two, assigned or not. <strong>Remaining</strong> is what the signup form shows:
      offered, minus volunteers assigned there, minus picks not yet assigned there.
    </p>
  </Dialog>
</template>

<style scoped>
.hint   { font-size: 0.82rem; color: var(--p-text-muted-color); line-height: 1.5; }
.tbl    { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin: 0.5rem 0; }
.tbl th, .tbl td { padding: 0.3rem 0.5rem; border-bottom: 1px solid var(--p-content-border-color);
                   text-align: left; }
.tbl th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em;
          color: var(--p-text-muted-color); }
.num    { text-align: right !important; }
.mono   { font-family: monospace; }
.muted  { color: var(--p-text-muted-color); }
.day-row td { font-weight: 600; padding-top: 0.6rem; }
</style>
