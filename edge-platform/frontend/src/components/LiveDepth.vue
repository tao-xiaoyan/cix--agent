<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'

type DepthState = 'live' | 'waiting' | 'stale' | 'disabled' | 'error' | 'auth' | 'forbidden'
const labels: Record<DepthState, string> = {
  live: 'Connected',
  waiting: 'Waiting for depth frames',
  stale: 'Depth frames are stale',
  disabled: 'Camera preview disabled',
  error: 'Depth preview failed',
  auth: 'Sign-in required',
  forbidden: 'Access denied',
}
const overviewLabels: Record<DepthState, string> = {
  live: '实时连接',
  waiting: '等待首帧',
  stale: '画面已过期',
  disabled: '未启用',
  error: '预览失败',
  auth: '请登录',
  forbidden: '无权访问',
}

const props = defineProps<{ active: boolean; authenticated: boolean; overview?: boolean }>()
const emit = defineEmits<{ unauthorized: [] }>()
const titleId = useId()
const state = ref<DepthState>('waiting')
const frame = ref<{ url: string; width: number; height: number } | null>(null)
const updatedAt = ref<string | null>(null)
const lastUpdated = computed(() => updatedAt.value
  ? new Date(updatedAt.value).toLocaleTimeString([], { fractionalSecondDigits: 3, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  : '—')
const displayState = computed(() => props.authenticated ? state.value : 'auth')
const stateLabel = computed(() => (props.overview ? overviewLabels : labels)[displayState.value])

function clearFrame() {
  if (frame.value) URL.revokeObjectURL(frame.value.url)
  frame.value = null
}

watch(() => props.active && props.authenticated, (enabled, _, onCleanup) => {
  if (!enabled) return
  let active = true
  let timer: number | undefined
  let request: AbortController | undefined
  let preview = false
  state.value = 'waiting'

  async function poll() {
    request = new AbortController()
    const { signal } = request
    const deadline = window.setTimeout(() => request?.abort(), 3000)
    try {
      const response = await fetch(preview ? '/api/v1/depth/preview.bmp' : '/api/v1/depth/status',
        { credentials: 'same-origin', cache: 'no-store', signal })
      if (!active) return
      if (response.status === 401) {
        state.value = 'auth'
        emit('unauthorized')
        return
      }
      if (response.status === 403) {
        state.value = 'forbidden'
        return
      }
      if (!preview) {
        if (!response.ok) throw new Error('Depth status unavailable')
        const status: { state: Exclude<DepthState, 'auth' | 'forbidden'> } = await response.json()
        if (!active) return
        signal.throwIfAborted()
        preview = status.state === 'live'
        if (!preview) state.value = status.state
      } else if (response.ok) {
        const blob = await response.blob()
        if (!active) return
        signal.throwIfAborted()
        const url = URL.createObjectURL(blob)
        const image = new Image()
        const releasePendingFrame = () => {
          image.removeAttribute('src')
          URL.revokeObjectURL(url)
        }
        signal.addEventListener('abort', releasePendingFrame, { once: true })
        try {
          image.src = url
          await image.decode()
          signal.throwIfAborted()
          clearFrame()
          frame.value = { url, width: image.naturalWidth, height: image.naturalHeight }
          updatedAt.value = response.headers.get('X-Depth-Updated-At')
          state.value = 'live'
        } finally {
          signal.removeEventListener('abort', releasePendingFrame)
          if (!signal.aborted && frame.value?.url !== url) URL.revokeObjectURL(url)
        }
      } else {
        const payload = await response.json()
        if (!active) return
        signal.throwIfAborted()
        const code = payload.error?.code
        state.value = code === 'depth_stale' ? 'stale'
          : code === 'depth_waiting' ? 'waiting' : code === 'depth_disabled' ? 'disabled' : 'error'
        preview = false
      }
    } catch {
      if (active) {
        state.value = 'error'
        preview = false
      }
    } finally {
      clearTimeout(deadline)
      if (active) {
        if (state.value !== 'live') clearFrame()
        if (state.value !== 'auth' && state.value !== 'forbidden') {
          timer = window.setTimeout(poll, preview ? 300 : 1000)
        }
      }
    }
  }

  onCleanup(() => {
    active = false
    clearTimeout(timer)
    request?.abort()
    clearFrame()
  })
  void poll()
}, { immediate: true })
</script>

<template>
  <section class="work-card live-depth" :class="{ 'overview-depth': overview }" :aria-labelledby="titleId">
    <div class="card-header">
      <div class="card-title">
        <span class="card-icon" aria-hidden="true">◈</span>
        <div><h2 :id="titleId">{{ overview ? '深度相机' : 'Live Depth' }}</h2><small>Orbbec Gemini 335</small></div>
      </div>
      <span class="state-label" :data-state="displayState === 'live' ? 'succeeded' : 'idle'" role="status">
        {{ stateLabel }}
      </span>
    </div>
    <div class="live-depth-body">
      <div class="live-depth-frame">
        <img v-if="frame" :src="frame.url" alt="Orbbec Gemini 335 live depth frame" />
        <a v-else-if="displayState === 'auth'" href="#overview">Sign in via Overview Dashboard</a>
        <p v-else-if="displayState === 'waiting'">{{ overview ? '相机可能离线或正在启动。' : 'Camera may be offline or starting up.' }}</p>
        <p v-else>{{ stateLabel }}</p>
      </div>
      <dl class="live-depth-meta">
        <div><dt>{{ overview ? '预览分辨率' : 'Preview resolution' }}</dt><dd>{{ frame ? `${frame.width} × ${frame.height}` : '—' }}</dd></div>
        <div><dt>{{ overview ? '最近成功更新' : 'Last successful frame' }}</dt><dd><time v-if="updatedAt" :datetime="updatedAt">{{ lastUpdated }}</time><template v-else>—</template></dd></div>
      </dl>
    </div>
  </section>
</template>
