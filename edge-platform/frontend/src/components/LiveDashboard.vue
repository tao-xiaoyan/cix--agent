<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import LiveDepth from './LiveDepth.vue'

type Event = { event_id: string; timestamp: string; event_type: string; source: string; confidence: number | null; payload: Record<string, any> }
type Channel = { name: string; unit: string | null; sampling_rate_hz: number | null; samples: (number | null)[]; quality: string }
type Status = { device_id: string; pending_upload_count: number; sensors: Record<string, string> }
const props = defineProps<{ active: boolean }>()
const authenticated = defineModel<boolean>('authenticated', { required: true })
const token = ref(sessionStorage.getItem('edge-token') || 'local-demo-token')
const emit = defineEmits<{ connection: [state: string] }>()
const connection = ref('连接中')
watch(connection, state => emit('connection', state), { immediate: true })
const status = ref<Status | null>(null)
const now = ref(new Date())
const lastSeen = ref<Date | null>(null)
const sleep = ref<Event | null>(null)
const pose = ref<Event | null>(null)
const metrics = ref<Event | null>(null)
const channels = ref<Channel[]>([])
const anomalies = ref<Event[]>([])
const audio = ref<Event[]>([])
const tasks = ref<any[]>([])
const results = ref<Event[]>([])
const decisions = ref<Event[]>([])
const points = ref<Record<string, (number | null)[]>>({})
const seen = new Set<string>()
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
let chartObserver: ResizeObserver | undefined
let socket: WebSocket | null = null
let retry: number | undefined
let clockTimer: number | undefined
let statusTimer: number | undefined
let stopped = false
const clock = computed(() => now.value.toLocaleString('zh-CN', { hour12: false }))
const freshness = computed(() => lastSeen.value ? `${Math.round((now.value.getTime() - lastSeen.value.getTime()) / 1000)} 秒前` : '暂无数据')
const cloud = computed(() => status.value?.sensors.cloud === 'connected' ? '已连接' : status.value?.sensors.cloud === 'disconnected' ? '离线 · 本地缓存' : '未配置')
const source = computed(() => sleep.value?.source === 'mock' ? 'MOCK 模拟数据' : sleep.value ? 'REAL / 模型数据' : '等待数据')

function requireLogin() {
  authenticated.value = false
  if (retry) clearTimeout(retry)
  if (socket) {
    socket.onclose = null
    socket.close()
    socket = null
  }
  connection.value = '会话已过期 · 请重新连接'
}

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
  if (event.event_type === 'cloud_analysis_result') results.value = [event, ...results.value].slice(0, 5)
  if (event.event_type === 'agent_decision') decisions.value = [event, ...decisions.value].slice(0, 5)
}
async function refresh() {
  try {
    const [statusResponse, eventsResponse] = await Promise.all([fetch('/api/v1/device/status'), fetch('/api/v1/events/recent?limit=100')])
    if (!authenticated.value) return
    if ([statusResponse, eventsResponse].some(response => response.status === 401)) {
      requireLogin()
      return
    }
    const taskResponse = await fetch('/api/v1/analysis/tasks/local')
    if (!authenticated.value) return
    if (taskResponse.status === 401) {
      requireLogin()
      return
    }
    if (taskResponse.ok) tasks.value = (await taskResponse.json()).items
    if (statusResponse.ok) status.value = await statusResponse.json()
    if (eventsResponse.ok) { const page = await eventsResponse.json(); for (const event of [...page.items].reverse()) consume(event) }
  } catch { connection.value = '服务不可用' }
}
async function login() {
  const response = await fetch('/api/v1/session', { method: 'POST', headers: { Authorization: `Bearer ${token.value}` } }).catch(() => null)
  if (!response) { connection.value = '服务不可用'; return }
  authenticated.value = response.ok
  if (!response.ok) {
    connection.value = response.status === 401 || response.status === 403 ? '认证失败' : '服务不可用'
    return
  }
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
  socket.onclose = () => { if (!stopped && authenticated.value) { connection.value = '已断开 · 重连中'; retry = window.setTimeout(connect, 2000) } }
  socket.onerror = () => socket?.close()
}
onMounted(() => {
  if (chartEl.value) {
    chartObserver = new ResizeObserver(() => {
      const element = chartEl.value
      if (!element?.clientWidth || !element.clientHeight) return
      if (chart) {
        chart.resize()
      } else {
        chart = echarts.init(element)
        renderChart()
      }
    })
    chartObserver.observe(chartEl.value)
  }
  clockTimer = window.setInterval(() => { now.value = new Date() }, 1000)
  statusTimer = window.setInterval(() => { if (authenticated.value) void refresh() }, 5000)
  void login()
})

onUnmounted(() => { stopped = true; socket?.close(); chart?.dispose(); chartObserver?.disconnect(); if (retry) clearTimeout(retry); if (clockTimer) clearInterval(clockTimer); if (statusTimer) clearInterval(statusTimer) })
</script>

<template>
  <div class="dashboard">
    <header class="topbar"><div class="brand"><div class="mark">✦</div><div><small>EDGE HEALTH / LIVE</small><h1>端侧实时监测</h1></div></div><div class="top-meta"><span class="tag mock">{{ source }}</span><span class="tag" :class="connection === '实时连接' ? 'good' : 'warn'">● {{ connection }}</span><span class="tag" :class="cloud === '已连接' ? 'good' : 'warn'">云端 {{ cloud }}</span><span class="clock">{{ clock }}<small>本地时间</small></span></div></header>
    <div v-if="!authenticated" class="auth"><div class="auth-box"><h2>本地监测访问</h2><p>输入本地操作令牌以连接实时监测。</p><input v-model="token" type="password" aria-label="本地操作令牌"/><button @click="login">连接设备</button><small>演示令牌：local-demo-token；部署时请设置 EDGE_OPERATOR_TOKEN。</small></div></div>
    <div v-show="authenticated" class="dashboard-content">
      <div class="summary"><div><small>设备 ID</small><strong>{{ status?.device_id || '连接中' }}</strong></div><div><small>最新事件</small><strong>{{ freshness }}</strong></div><div><small>待同步事件</small><strong>{{ status?.pending_upload_count ?? '—' }}</strong></div><div><small>监测模式</small><strong>本地 Mock / 规则调度</strong></div></div>
      <div class="grid">
        <section class="column">
          <LiveDepth :active="props.active" :authenticated="authenticated" overview @unauthorized="requireLogin" />
          <article class="panel"><div class="panel-head"><h2>人体姿态</h2><span>POSE</span></div><div class="pose"><div class="pose-icon">◇</div><div><strong>{{ pose?.payload.status === 'available' ? '检测到模拟人体' : '暂无检测' }}</strong><p>{{ pose?.payload.persons?.length ?? 0 }} 人 · {{ pose?.payload.coordinate_system || '坐标未知' }}</p><small>模拟关键点，仅用于界面验证</small></div></div></article><article class="panel"><div class="panel-head"><h2>当前睡姿</h2><span>POSTURE</span></div><div class="sleep"><strong>{{ sleep?.payload.label || '未知' }}</strong><div>置信度<b>{{ sleep?.confidence == null ? '未知' : `${Math.round(sleep.confidence * 100)}%` }}</b></div></div></article></section>
        <section class="column"><article class="panel signal"><div class="panel-head"><div><h2>生理信号实时曲线</h2><p>通道由输入元数据提供 · 最近 120 个采样点</p></div><span>LIVE SIGNAL</span></div><div ref="chartEl" class="chart"></div><div v-if="!channels.length" class="empty">等待生理信号窗口</div><div class="channels"><div v-for="channel in channels" :key="channel.name" class="channel"><i></i><div><strong>{{ channel.name }}</strong><small>{{ channel.unit || '单位未知' }} · {{ channel.sampling_rate_hz == null ? '采样率未知' : `${channel.sampling_rate_hz} Hz` }}</small></div><span>{{ channel.quality }}</span></div></div><p class="hint">PPG / GSR / IMU 为模拟候选通道，不代表实际设备能力。</p></article></section>
        <section class="column feeds"><article class="panel feed"><div class="panel-head"><h2>异常事件</h2><span>ALERTS</span></div><div v-if="!anomalies.length" class="empty">暂无异常事件</div><div v-for="event in anomalies" :key="event.event_id" class="feed-item alert"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.description }}</strong><span>{{ event.payload.severity }} / {{ event.payload.code }}</span></div></article><article class="panel feed"><div class="panel-head"><h2>音频事件</h2><span>AUDIO</span></div><div v-if="!audio.length" class="empty">暂无音频事件</div><div v-for="event in audio" :key="event.event_id" class="feed-item"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.label || '未知类别' }}</strong><span>{{ event.payload.status }}</span></div></article><article class="panel feed"><div class="panel-head"><h2>规则决策日志</h2><span>RULES</span></div><div v-if="!decisions.length" class="empty">等待规则触发</div><div v-for="event in decisions" :key="event.event_id" class="feed-item"><small>{{ new Date(event.timestamp).toLocaleTimeString() }} · {{ event.source.toUpperCase() }}</small><strong>{{ event.payload.decision }}</strong><span>{{ event.payload.rationale }}</span><em>{{ event.payload.execution_status }} · {{ event.payload.actions?.join(' → ') }}</em></div></article></section>
      </div>
      <section class="panel"><h2>云端 Mock 分析闭环</h2>
        <div v-for="task in tasks" :key="task.task_key">异常 {{ task.task_key }} → 任务 {{ task.cloud_task_id || '等待上传' }} · {{ task.status }} {{ task.last_error }}</div>
        <div v-for="result in results" :key="result.event_id">结果 {{ result.event_id }} · 任务 {{ result.payload.task_id }} · {{ result.payload.summary }}</div>
      </section>
      <footer class="metrics"><div><small>CPU 使用率</small><strong>{{ metrics?.payload.cpu_percent == null ? '未采集' : `${metrics.payload.cpu_percent}%` }}</strong></div><div><small>内存占用</small><strong>{{ metrics?.payload.memory_used_bytes == null ? '未采集' : `${Math.round(metrics.payload.memory_used_bytes / 1048576)} MiB` }}</strong></div><div><small>NPU 状态</small><strong>未接入</strong></div><div><small>推理 FPS</small><strong>未测量</strong></div><div><small>模型运行状态</small><strong>Mock 模式</strong></div></footer>
    </div>
  </div>
</template>

<style scoped>
.dashboard { container-type: inline-size; overflow-wrap: anywhere; }
.topbar{min-height:88px;border-bottom:1px solid #263949;padding:18px 28px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:14px}.mark{background:#1d5558;color:#63ddc8;border-radius:12px;padding:7px 13px;font-size:28px}.brand small,.panel-head span,.clock small{color:#83a0ae;font-size:10px;letter-spacing:.16em}.brand h1{font-size:22px;margin:3px 0}.top-meta{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.tag{padding:7px 11px;border:1px solid #3a4e5d;border-radius:100px;font-size:12px;color:#bed0d9}.tag.mock{background:#182e46;color:#a0c7ff}.tag.good{background:#123d36;color:#70e2be}.tag.warn{background:#3b2c20;color:#f3bf74}.clock{font-size:14px;font-weight:600;margin-left:10px}.clock small{display:block;text-align:right;margin-top:4px}.dashboard-content{max-width:1920px;margin:auto;padding:22px 28px}.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}.summary>div,.panel,.metrics{background:#111f2d;border:1px solid #2a3d4e;border-radius:15px}.summary>div{padding:14px 18px}.summary small,.metrics small{display:block;color:#8ea7b6;font-size:11px;margin-bottom:8px}.summary strong{font-size:15px}.grid{display:grid;grid-template-columns:minmax(250px,1fr) minmax(430px,2fr) minmax(280px,1.08fr);gap:16px}.column{display:flex;flex-direction:column;gap:16px;min-width:0}.panel{padding:18px;overflow:hidden}.panel-head{display:flex;justify-content:space-between;align-items:start;gap:8px;margin-bottom:16px}.panel-head h2{font-size:15px;margin:0}.panel-head p{font-size:11px;color:#829baa;margin:5px 0 0}.pose{display:flex;align-items:center;gap:17px;min-height:88px}.pose-icon{width:70px;height:70px;border-radius:50%;background:#16413f;color:#55d9c6;display:grid;place-items:center;font-size:42px}.pose strong{font-size:14px}.pose p,.pose small{font-size:11px;color:#90aabb}.sleep{display:flex;justify-content:space-between;align-items:end}.sleep>strong{font-size:32px;color:#6bdfcb}.sleep div{text-align:right;color:#96aebe;font-size:11px}.sleep b{display:block;color:#e3f3f6;font-size:16px}.signal{min-height:580px;height:100%}.chart{height:355px;width:100%}.channels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.channel{display:flex;align-items:center;gap:7px;padding:10px;background:#182c3d;border:1px solid #304a5b;border-radius:9px;min-width:0}.channel i{width:8px;height:8px;background:#56dbc9;border-radius:50%;flex:none;box-shadow:0 0 9px #56dbc9}.channel:nth-child(2) i{background:#efb765;box-shadow:0 0 9px #efb765}.channel:nth-child(3) i{background:#9da5fa;box-shadow:0 0 9px #9da5fa}.channel div{min-width:0}.channel strong,.channel small{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.channel strong{font-size:12px}.channel small,.channel span{font-size:9px;color:#8da6b6}.channel span{margin-left:auto;color:#82dcca}.hint{font-size:11px;color:#86a0b1;margin-top:16px}.feed{flex:1;min-height:155px;max-height:240px;overflow-y:auto}.feed-item{border-left:2px solid #51c7bd;background:#182c3d;padding:8px 10px;margin-bottom:8px;border-radius:0 8px 8px 0}.feed-item.alert{border-color:#f0b76b}.feed-item>*{display:block}.feed-item small{font-size:9px;color:#91aabb}.feed-item strong{font-size:12px;margin:5px 0}.feed-item span,.feed-item em{font-size:10px;color:#9cb3c0;font-style:normal}.feed-item em{color:#85d3c9;margin-top:5px}.empty{padding:35px 0;text-align:center;color:#7893a4;font-size:12px}.metrics{display:grid;grid-template-columns:repeat(5,1fr);margin-top:17px;padding:15px 0}.metrics>div{padding:3px 18px;border-right:1px solid #2c4555}.metrics>div:last-child{border:0}.metrics strong{font-size:15px}.auth{min-height:320px;display:grid;place-items:center}.auth-box{width:min(420px,90vw);background:#142637;border:1px solid #365466;border-radius:16px;padding:30px}.auth-box h2{margin:0}.auth-box p,.auth-box small{font-size:12px;color:#94acbb}.auth-box input{display:block;width:100%;margin:20px 0 10px;padding:12px;background:#0b1724;border:1px solid #476072;border-radius:8px;color:white}.auth-box button{width:100%;background:#53d8c5;color:#0a2830;font-weight:700;border:0;border-radius:8px;padding:12px;cursor:pointer}@container (max-width:1250px){.grid{grid-template-columns:minmax(250px,1fr) minmax(400px,1.7fr)}.feeds{grid-column:1/-1;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}}@container (max-width:760px){.topbar{padding:16px;align-items:flex-start;flex-direction:column}.dashboard-content{padding:14px}.summary{grid-template-columns:repeat(2,1fr)}.grid,.feeds{display:block}.column,.feeds .panel{margin-bottom:14px}.channels{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.metrics>div{border-bottom:1px solid #2c4555}}

</style>
