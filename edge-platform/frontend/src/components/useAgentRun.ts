import { computed, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'

export type RunStatus = 'submitted' | 'running' | 'completed' | 'failed' | 'cancelled'

export type DepthEvidence = {
  predicted_posture: 'supine' | 'left' | 'right'
  dataset_id: string
  subject_id: number
  pose_id: number
  model_version: string
  inference_device: string
  execution_id: string
  evidence_reference: string
  input_sha256: string
  model_sha256: string
  execution_status: 'succeeded'
}

export type AgentRun = {
  run_id: string
  query: string
  status: RunStatus
  agent_run_id: string | null
  submitted_at: string
  finished_at: string | null
  selected_skill: 'depth_posture_analysis' | null
  answer: string | null
  error: string | null
  evidence: DepthEvidence | null
}

const endpoint = '/api/v1/agent/runs'
const storedRun = 'edge-workbench-run-id'

class RunRequestError extends Error {
  constructor(readonly status: number, readonly code?: string) {
    super(`Agent request failed (${status})`)
  }
}

async function requestRun(path = '', init?: RequestInit): Promise<AgentRun> {
  const response = await fetch(endpoint + path, {
    ...init,
    credentials: 'same-origin',
    cache: 'no-store',
    signal: AbortSignal.timeout(8000),
  })
  if (!response.ok) {
    const payload = response.status === 503 ? await response.json().catch(() => null) : null
    throw new RunRequestError(response.status, payload?.error?.message)
  }
  return response.json()
}

export async function readAgentRun(id: string): Promise<AgentRun> {
  try {
    return await requestRun('/' + encodeURIComponent(id))
  } catch (error) {
    const status = error instanceof RunRequestError ? error.status : null
    if (status === 401 || status === 403) throw new Error('Sign in via Overview Dashboard to read the run.')
    if (status === 404) throw new Error('The edge service no longer has this run.')
    if (status === 422) throw new Error('Enter a valid Run ID.')
    throw new Error('The edge service could not load this run.')
  }
}

export function useAgentRun(active: Ref<boolean>) {
  const run = ref<AgentRun | null>(null)
  const id = ref(sessionStorage.getItem(storedRun))
  const unavailable = ref('')
  const needsAuth = ref(false)
  const loading = ref(false)
  const cancelling = ref(false)
  let timer: number | undefined
  let stopped = false

  const ongoing = computed(() => !!id.value && (!run.value || run.value.status === 'submitted' || run.value.status === 'running'))
  const status = computed(() => unavailable.value ? 'unavailable' : run.value?.status ?? 'idle')

  function remember(value: string | null) {
    id.value = value
    if (value) sessionStorage.setItem(storedRun, value)
    else sessionStorage.removeItem(storedRun)
  }

  function setError(error: unknown, method: 'GET' | 'POST' | 'DELETE') {
    const code = error instanceof RunRequestError ? error.status : null
    const disabled = error instanceof RunRequestError && code === 503 && error.code === 'depth_runner_unavailable'
    needsAuth.value = code === 401 || code === 403
    if (needsAuth.value) {
      unavailable.value = code === 401 ? 'Sign in via Overview Dashboard, then retry.' : 'This origin is not authorized to access the Agent.'
    } else if (code === 404 && method === 'GET') {
      unavailable.value = 'This run is unavailable. The edge service may have restarted or expired its record.'
      remember(null)
      run.value = null
    } else if (code === 429) {
      unavailable.value = 'The edge service is busy with another depth run.'
    } else if (disabled) {
      unavailable.value = 'The real depth runner is unavailable on this edge service.'
    } else if (!code || code >= 500) {
      unavailable.value = 'The edge service did not respond. Run status is unavailable until it reconnects.'
    } else {
      unavailable.value = `The edge service rejected the request (${code}).`
    }
    if (method === 'POST' && ((code !== null && code < 500) || disabled)) remember(null)
  }

  function schedule() {
    window.clearTimeout(timer)
    if (!stopped && active.value && ongoing.value && !needsAuth.value) {
      timer = window.setTimeout(() => { void refresh() }, 2000)
    }
  }

  async function refresh() {
    if (!id.value || loading.value || cancelling.value) return
    window.clearTimeout(timer)
    loading.value = true
    try {
      run.value = await requestRun('/' + id.value)
      unavailable.value = ''
      needsAuth.value = false
    } catch (error) {
      setError(error, 'GET')
    } finally {
      loading.value = false
      schedule()
    }
  }

  async function submit(query: string) {
    if (ongoing.value || loading.value || cancelling.value) return
    window.clearTimeout(timer)
    loading.value = true
    unavailable.value = ''
    needsAuth.value = false
    run.value = null
    try {
      const runId = crypto.randomUUID()
      remember(runId)
      run.value = await requestRun('', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': runId },
        body: JSON.stringify({ query }),
      })
    } catch (error) {
      setError(error, 'POST')
    } finally {
      loading.value = false
      schedule()
    }
  }

  async function cancel() {
    if (!id.value || !run.value || !ongoing.value || loading.value || cancelling.value) return
    window.clearTimeout(timer)
    cancelling.value = true
    try {
      run.value = await requestRun('/' + id.value, { method: 'DELETE' })
      unavailable.value = ''
      needsAuth.value = false
    } catch (error) {
      setError(error, 'DELETE')
    } finally {
      cancelling.value = false
      schedule()
    }
  }

  function reconnect() {
    if (active.value && ongoing.value) void refresh()
  }

  watch(active, shown => {
    if (shown) reconnect()
    else window.clearTimeout(timer)
  })
  onMounted(() => {
    window.addEventListener('online', reconnect)
    reconnect()
  })
  onUnmounted(() => {
    stopped = true
    window.clearTimeout(timer)
    window.removeEventListener('online', reconnect)
  })

  return { id, run, status, unavailable, needsAuth, loading, ongoing, cancelling, refresh, submit, cancel }
}
