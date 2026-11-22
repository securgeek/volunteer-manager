<script setup>
import { ref } from 'vue'
import FileUpload from 'primevue/fileupload'
import Button     from 'primevue/button'
import Message    from 'primevue/message'

const emit = defineEmits(['done', 'skipped'])

const import_file    = ref(null)
const import_result  = ref(null)
const import_loading = ref(false)

function on_file_select(event) {
  import_file.value   = event.files[0]
  import_result.value = null
}

async function submit() {
  if (!import_file.value) return
  import_loading.value = true
  import_result.value  = null
  const form = new FormData()
  form.append('file', import_file.value)
  try {
    const resp = await fetch('/api/setup/import_schedule', { method: 'POST', body: form })
    if (resp.ok) {
      const data = await resp.json()
      import_result.value = data
      if (data.inserted_slots > 0) {
        setTimeout(() => emit('done'), 1500)
      }
    } else {
      const body = await resp.json().catch(() => null)
      import_result.value = { error: body?.detail || 'Import failed.' }
    }
  } catch {
    import_result.value = { error: 'Unable to reach the server.' }
  } finally {
    import_loading.value = false
  }
}
</script>

<template>
  <div style="display:flex; justify-content:center; align-items:flex-start;
              min-height:80vh; padding-top:6rem;">
    <div style="width:38rem; display:flex; flex-direction:column; gap:1rem;">
      <div>
        <h2 style="margin:0;">Import Schedule</h2>
        <p style="color:var(--p-text-muted-color); margin:0.5rem 0 0;">
          Upload a CSV defining the schedule structure — days, times, and positions.
        </p>
      </div>

      <div style="font-size:0.85rem; color:var(--p-text-muted-color);
                  background:var(--p-content-background); border:1px solid var(--p-content-border-color);
                  border-radius:6px; padding:0.75rem 1rem; font-family:monospace; line-height:1.6;">
        Required columns (one row per schedule slot):<br>
        <strong>day_key, day_str, time_key, time_str, pos_key, pos_cat, pos_str</strong><br>
        Optional: <strong>date</strong> (YYYY-MM-DD — displayed as "Sat 4/26")<br><br>
        Example:<br>
        sat,Saturday,0800,0800-1200,sec1,Security,Security 1,2026-04-25<br>
        sat,Saturday,0800,0800-1200,reg1,Registration,Registration 1,2026-04-25
      </div>

      <FileUpload mode="basic" accept=".csv,text/csv" :maxFileSize="5000000"
                  chooseLabel="Choose CSV…" :auto="false" @select="on_file_select" />

      <Message v-if="import_result?.error" severity="error" :closable="false">
        {{ import_result.error }}
      </Message>
      <Message v-else-if="import_result" severity="success" :closable="false">
        Imported {{ import_result.inserted_slots }} slots across
        {{ import_result.days }} day(s), {{ import_result.times }} timeslot(s),
        {{ import_result.positions }} position(s).
        <span v-if="import_result.skipped > 0">({{ import_result.skipped }} skipped)</span>
      </Message>
      <div v-if="import_result?.errors?.length"
           style="font-size:0.8rem; color:var(--p-text-muted-color);">
        <div v-for="e in import_result.errors" :key="e">{{ e }}</div>
      </div>

      <div style="display:flex; gap:0.5rem;">
        <Button label="Import" :loading="import_loading" :disabled="!import_file" @click="submit" />
        <Button label="Skip for now" severity="secondary" @click="emit('skipped')" />
      </div>
    </div>
  </div>
</template>
