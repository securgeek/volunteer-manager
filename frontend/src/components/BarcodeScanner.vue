<script setup>
import { ref, onMounted, onUnmounted, useTemplateRef } from 'vue'
import Button    from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message   from 'primevue/message'

const props = defineProps({
  // Ignore a repeat of the same code for this long, so one barcode held in frame
  // does not fire a burst of checkouts.
  repeatMs: { type: Number, default: 2500 },
})
const emit = defineEmits(['scanned'])

const video_ref  = useTemplateRef('video')
const manual     = ref('')
const cam_state  = ref('starting')   // 'starting' | 'running' | 'unavailable'
const cam_err    = ref('')
const torch_on   = ref(false)
const has_torch  = ref(false)

let controls   = null   // IScannerControls from ZXing
let track      = null   // MediaStreamTrack, kept for the torch toggle
let last_code  = ''
let last_at    = 0

function submit(code) {
  const text = (code ?? '').trim()
  if (!text) return
  const now = Date.now()
  if (text === last_code && now - last_at < props.repeatMs) return
  last_code = text
  last_at   = now
  emit('scanned', text)
}

function submit_manual() {
  const text = manual.value.trim()
  if (!text) return
  manual.value = ''
  // Typed codes are deliberate: bypass the repeat guard so the same radio can be
  // re-entered immediately after a correction.
  last_code = ''
  emit('scanned', text)
}

async function start() {
  cam_state.value = 'starting'
  cam_err.value   = ''
  try {
    // Loaded on demand — ZXing is a few hundred KB and only the scanner needs it.
    const [{ BrowserMultiFormatReader }, { DecodeHintType, BarcodeFormat }] =
      await Promise.all([import('@zxing/browser'), import('@zxing/library')])

    const hints = new Map([
      [DecodeHintType.POSSIBLE_FORMATS, [
        BarcodeFormat.CODE_128, BarcodeFormat.CODE_39, BarcodeFormat.ITF,
        BarcodeFormat.EAN_13, BarcodeFormat.EAN_8,
        BarcodeFormat.UPC_A, BarcodeFormat.UPC_E,
        BarcodeFormat.QR_CODE, BarcodeFormat.DATA_MATRIX,
      ]],
      [DecodeHintType.TRY_HARDER, true],
    ])

    const reader = new BrowserMultiFormatReader(hints, { delayBetweenScanAttempts: 150 })
    controls = await reader.decodeFromConstraints(
      { video: { facingMode: { ideal: 'environment' } } },
      video_ref.value,
      (result) => { if (result) submit(result.getText()) },
    )
    track = video_ref.value?.srcObject?.getVideoTracks?.()[0] ?? null
    has_torch.value = !!track?.getCapabilities?.().torch
    cam_state.value = 'running'
  } catch (e) {
    cam_state.value = 'unavailable'
    cam_err.value   = describe(e)
  }
}

// getUserMedia failures are indistinguishable from the message alone, and the
// difference decides what the person should do about it.
function describe(e) {
  const name = e?.name || ''
  if (name === 'NotAllowedError')
    return 'Camera permission was refused. Allow it in your browser settings, or type the barcode below.'
  if (name === 'NotFoundError' || name === 'OverconstrainedError')
    return 'No camera was found on this device. Type the barcode below.'
  if (name === 'NotReadableError')
    return 'The camera is in use by another app. Close it and reopen this, or type the barcode below.'
  if (!window.isSecureContext)
    return 'The camera needs a secure (HTTPS) connection. Type the barcode below.'
  return 'The camera could not be started. Type the barcode below.'
}

function stop() {
  try { controls?.stop() } catch { /* already stopped */ }
  controls = null
  // ZXing stops its own stream, but a torch left on outlives the element otherwise.
  try { track?.applyConstraints({ advanced: [{ torch: false }] }) } catch { /* no torch */ }
  track = null
  has_torch.value = false
}

async function toggle_torch() {
  if (!track) return
  try {
    torch_on.value = !torch_on.value
    await track.applyConstraints({ advanced: [{ torch: torch_on.value }] })
  } catch {
    torch_on.value = false
  }
}

onMounted(start)
onUnmounted(stop)
defineExpose({ stop, restart: async () => { stop(); await start() } })
</script>

<template>
  <div class="scan-wrap">
    <div class="video-box">
      <video ref="video" playsinline muted autoplay class="video" />
      <div v-if="cam_state === 'starting'" class="overlay">
        <i class="pi pi-spin pi-spinner" style="font-size:1.5rem;" />
        <span>Starting camera…</span>
      </div>
      <div v-else-if="cam_state === 'running'" class="reticle" />
      <Button v-if="cam_state === 'running' && has_torch"
              class="torch" size="small" severity="secondary"
              :icon="torch_on ? 'pi pi-bolt' : 'pi pi-bolt'"
              :label="torch_on ? 'Light off' : 'Light on'" @click="toggle_torch" />
    </div>

    <Message v-if="cam_state === 'unavailable'" severity="warn" :closable="false"
             style="margin:0;">{{ cam_err }}</Message>

    <div class="manual-row">
      <InputText v-model="manual" placeholder="Or type / scan the barcode"
                 autocomplete="off" autocapitalize="off" spellcheck="false"
                 @keyup.enter="submit_manual" style="flex:1;" />
      <Button label="Use" :disabled="!manual.trim()" @click="submit_manual" />
    </div>
  </div>
</template>

<style scoped>
.scan-wrap { display: flex; flex-direction: column; gap: 0.75rem; }
.video-box {
  position: relative;
  width: 100%;
  aspect-ratio: 4 / 3;
  max-height: 45vh;
  background: #000;
  border-radius: 8px;
  overflow: hidden;
}
.video { width: 100%; height: 100%; object-fit: cover; display: block; }
.overlay {
  position: absolute; inset: 0;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 0.5rem; color: #fff; font-size: 0.9rem;
}
/* A guide for where to hold the barcode; purely visual, the decoder reads the full frame. */
.reticle {
  position: absolute; left: 8%; right: 8%; top: 32%; height: 36%;
  border: 2px solid rgba(255, 255, 255, 0.85);
  border-radius: 6px;
  box-shadow: 0 0 0 100vmax rgba(0, 0, 0, 0.25);
}
.torch { position: absolute; right: 0.5rem; bottom: 0.5rem; }
.manual-row { display: flex; gap: 0.5rem; }
</style>
