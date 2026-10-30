<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import * as echarts from 'echarts'

type Event = { event_id: string; timestamp: string; event_type: string; source: string; confidence: number | null; payload: Record<string, any> }
type Channel = { name: string; unit: string | null; sampling_rate_hz: number | null; samples: (number | null)[]; quality: string }
type Status = { device_id: string; pending_upload_count: number; sensors: Record<string, string> }
const token = ref(sessionStorage.getItem('edge-token') || 'local-demo-token')
const authenticated = ref(false)
const connection = ref('连接中')
const status = ref<Status | null>(null)
const now = ref(new Date())
const lastSeen = ref<Date | null>(null)
const sleep = ref<Event | null>(null)
const pose = ref<Event | null>(null)
const metrics = ref<Event | null>(null)
const channels = ref<Channel[]>([])
const anomalies = ref<Event[]>([])
const audio = ref<Event[]>([])
const decisions = ref<Event[]>([])
const points = ref<Record<string, (number | null)[]>>({})
const seen = new Set<string>()
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let socket: WebSocket | null = null
let retry: number | undefined
let clockTimer: number | undefined
let statusTimer: number | undefined
let stopped = false
const clock = computed(() => now.value.toLocaleString('zh-CN', { hour12: false }))
const freshness = computed(() => lastSeen.value ? `${Math.round((now.value.getTime() - lastSeen.value.getTime()) / 1000)} 秒前` : '暂无数据')
const cloud = computed(() => status.value?.sensors.cloud === 'connected' ? '已连接' : status.value?.sensors.cloud === 'disconnected' ? '离线 · 本地缓存' : '未配置')
const source = computed(() => sleep.value?.source === 'mock' ? 'MOCK 模拟数据' : sleep.value ? 'REAL / 模型数据' : '等待数据')

function renderChart() {
  if (!chart) return
  chart.setOption({ color: ['#51dac9', '#f1b864', '#929ff5'], tooltip: { trigger: 'axis' }, legend: { top: 4, textStyle: { color: '#9db4c3' } }, grid: { left: 42, right: 20, top: 48, bottom: 24 },
    xAxis: { type: 'category', data: Array.from({ length: 120 }, (_, index) => index), axisLabel: { show: false }, axisLine: { lineStyle: { color: '#3d5267' } } },
    yAxis: { type: 'value', scale: true, axisLabel: { color: '#89a1b2' }, splitLine: { lineStyle: { color: '#294057' } } },
    series: channels.value.map(channel => ({ name: channel.name, type: 'line', showSymbol: false, connectNulls: false, data: points.value[channel.name] || [] })) }, true)
}
function consume(event: Event) {
  if (seen.has(event.event_id)) return
  seen.add(event.event_id)
  if (seen.size > 1000) seen.clear()
  lastSeen.value = new Date(event.timestamp)
  if (event.event_type === 'physiological_window') {
    channels.value = event.payload.channels || []
    for (const channel of channels.value) points.value[channel.name] = [...(points.value[channel.name] || []), ...channel.samples].slice(-120)
    renderChart()
  }
  if (event.event_type === 'pose_estimation') pose.value = event
  if (event.event_type === 'sleep_posture') sleep.value = event
  if (event.event_type === 'device_metrics') metrics.value = event
  if (event.event_type === 'anomaly_event') anomalies.value = [event, ...anomalies.value].slice(0, 5)
  if (event.event_type === 'audio_event') audio.value = [event, ...audio.value].slice(0, 5)
  if (event.event_type === 'agent_decision') decisions.value = [event, ...decisions.value].slice(0, 5)
}
async function refresh() {
  try {
    const [statusResponse, eventsResponse] = await Promise.all([fetch('/api/v1/device/status'), fetch('/api/v1/events/recent?limit=100')])
    if (statusResponse.ok) status.value = await statusResponse.json()
    if (eventsResponse.ok) { const page = await eventsResponse.json(); for (const event of [...page.items].reverse()) consume(event) }
  } catch { connection.value = '服务不可用' }
}
async function login() {
  const response = await fetch('/api/v1/session', { method: 'POST', headers: { Authorization: `Bearer ${token.value}` } })
  authenticated.value = response.ok
  if (!response.ok) { connection.value = '认证失败'; return }
  sessionStorage.setItem('edge-token', token.value)
  await refresh()
  connect()
}
function connect() {
  if (stopped || !authenticated.value) return
  connection.value = '连接中'
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/realtime`)
  socket.onopen = () => { connection.value = '实时连接'; void refresh() }
  socket.onmessage = message => { const packet = JSON.parse(message.data); if (packet.kind === 'event' && packet.event) consume(packet.event); if (packet.kind === 'gap') void refresh() }
  socket.onclose = () => { connection.value = '已断开 · 重连中'; if (!stopped) retry = window.setTimeout(connect, 2000) }
  socket.onerror = () => socket?.close()
}
onMounted(() => {
  if (chartEl.value) chart = echarts.init(chartEl.value)
  window.addEventListener('resize', resize)
  clockTimer = window.setInterval(() => { now.value = new Date() }, 1000)
  statusTimer = window.setInterval(() => { if (authenticated.value) void refresh() }, 5000)
  void login()
})
function resize() { chart?.resize() }
onUnmounted(() => { stopped = true; socket?.close(); chart?.dispose(); window.removeEventListener('resize', resize); if (retry) clearTimeout(retry); if (clockTimer) clearInterval(clockTimer); if (statusTimer) clearInterval(statusTimer) })
</script>

<template>
  <div class="shell">
    <header class="topbar"><div class="brand"><div class="mark">✦</div><div><small>EDGE HEALTH / LIVE</small><h1>端侧实时监测</h1></div></div><div class="top-meta"><span class="tag mock">{{ source }}</span><span class="tag" :class="connection === '实时连接' ? 'good' : 'warn'">● {{ connection }}</span><span class="tag" :class="cloud === '已连接' ? 'good' : 'warn'">云端 {{ cloud }}</span><span class="clock">{{ clock }}<small>本地时间</small></span></div></header>
    <div v-if="!authenticated" class="auth"><div class="auth-box"><h2>本地监测访问</h2><p>输入本地操作令牌以连接实时监测。</p><input v-model="token" type="password" aria-label="本地操作令牌"/><button @click="login">连接设备</button><small>演示令牌：local-demo-token；部署时请设置 EDGE_OPERATOR_TOKEN。</small></div></div>
    <main v-else>
      <div class="summary"><div><small>设备 ID</small><strong>{{ status?.device_id || '连接中' }}</strong></div><div><small>最新事件</small><strong>{{ freshness }}</strong></div><div><small>待同步事件</small><strong>{{ status?.pending_upload_count ?? '—' }}</strong></div><div><small>监测模式</small><strong>本地 Mock / 规则调度</strong></div></div>
      <div class="grid">
        <section class="column"><article class="panel"><div class="panel-head"><h2>红外影像</h2><span>IR CAMERA</span></div><div class="camera"><div class="camera-icon">▧</div><strong>摄像头未接入</strong><p>当前仅显示模拟推理事件，无视频画面</p><small>NO LIVE FEED</small></div></article><article class="panel"><div class="panel-head"><h2>人体姿态</h2><span>POSE</span></div><div class="pose"><div class="pose-icon">◇</div><div><strong>{{ pose?.payload.status === 'available' ? '检测到模拟人体' : '暂无检测' }}</strong><p>{{ pose?.payload.persons?.length ?? 0 }} 人 · {{ pose?.payload.coordinate_system || '坐标未知' }}</p><small>模拟关键点，仅用于界面验证</small></div></div></article><article class="panel"><div class="panel-head"><h2>当前睡姿</h2><span>POSTURE</span></div><div class="sleep"><strong>{{ sleep?.payload.label || '未知' }}</strong><div>置信度<b>{{ sleep?.confidence == null ? '未知' : `${Math.round(sleep.confidence * 100)}%` }}</b></div></div></article></section>
        <section class="column"><article class="panel signal"><div class="panel-head"><div><h2>生理信号实时曲线</h2><p>通道由输入元数据提供 · 最近 120 个采样点</p></div><span>LIVE SIGNAL</span></div><div ref="chartEl" class="chart"></div><div v-if="!channels.length" class="empty">等待生理信号窗口</div><div class="channels"><div v-for="channel in channels" :key="channel.name" class="channel"><i></i><div><strong>{{ channel.name }}</strong><small>{{ channel.unit || '单位未知' }} · {{ channel.sampling_rate_hz == null ? '采样率未知' : `${channel.sampling_rate_hz} Hz` }}</small></div><span>{{ channel.quality }}</span></div></div><p class="hint">PPG / GSR / IMU 为模拟候选通道，不代表实际设备能力。</p></article></section>
        <section class="column feeds"><article class="panel feed"><div class="panel-head"><h2>异常事件</h2><span>ALERTS</span></div><div v-if="!anomalies.length" class="empty">暂无异常事件</div><div v-for="event in anomalies" :key="event.event_id" class="feed-item alert"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.description }}</strong><span>{{ event.payload.severity }} / {{ event.payload.code }}</span></div></article><article class="panel feed"><div class="panel-head"><h2>音频事件</h2><span>AUDIO</span></div><div v-if="!audio.length" class="empty">暂无音频事件</div><div v-for="event in audio" :key="event.event_id" class="feed-item"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.label || '未知类别' }}</strong><span>{{ event.payload.status }}</span></div></article><article class="panel feed"><div class="panel-head"><h2>规则决策日志</h2><span>RULES</span></div><div v-if="!decisions.length" class="empty">等待规则触发</div><div v-for="event in decisions" :key="event.event_id" class="feed-item"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.decision }}</strong><span>{{ event.payload.rationale }}</span><em>{{ event.payload.execution_status }} · {{ event.payload.actions?.join(' → ') }}</em></div></article></section>
      </div>
      <footer class="metrics"><div><small>CPU 使用率</small><strong>{{ metrics?.payload.cpu_percent == null ? '未采集' : `${metrics.payload.cpu_percent}%` }}</strong></div><div><small>内存占用</small><strong>{{ metrics?.payload.memory_used_bytes == null ? '未采集' : `${Math.round(metrics.payload.memory_used_bytes / 1048576)} MiB` }}</strong></div><div><small>NPU 状态</small><strong>未接入</strong></div><div><small>推理 FPS</small><strong>未测量</strong></div><div><small>模型运行状态</small><strong>Mock 模式</strong></div></footer>
    </main>
  </div>
</template>
