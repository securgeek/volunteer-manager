import { onUnmounted } from 'vue'

// Singleton SSE connection and handler registry.
let   _sse      = null
const _handlers = new Map()   // eventType -> Set<handler>

/**
 * Open the SSE connection and attach any handlers already registered
 * by components that mounted before the connection was established.
 */
export function startEvents() {
  if (_sse) return
  _sse = new EventSource('/api/events')
  for (const [type, handlers] of _handlers) {
    for (const fn of handlers) {
      _sse.addEventListener(type, fn)
    }
  }
}

/**
 * Close the SSE connection (e.g. on logout). Registered handlers are
 * preserved so they re-attach automatically on the next startEvents().
 */
export function stopEvents() {
  _sse?.close()
  _sse = null
}

/**
 * Composable for components that want to react to server-sent events.
 *
 * Usage:
 *   const { on } = useEvents()
 *   on('schedule', () => reload())
 *   on('checkin',  (e) => console.log(e.data))
 *
 * Handlers are removed automatically when the component is unmounted.
 */
export function useEvents() {
  const _registered = []  // [type, fn] pairs owned by this component instance

  function on(eventType, handler) {
    if (!_handlers.has(eventType)) _handlers.set(eventType, new Set())
    _handlers.get(eventType).add(handler)
    _sse?.addEventListener(eventType, handler)
    _registered.push([eventType, handler])
  }

  onUnmounted(() => {
    for (const [type, fn] of _registered) {
      _handlers.get(type)?.delete(fn)
      _sse?.removeEventListener(type, fn)
    }
  })

  return { on }
}
