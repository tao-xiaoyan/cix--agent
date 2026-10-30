<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import Chart from './Chart.vue'
import { api, type Event, type Device, type Task, type Page, type Dashboard } from './api'
const tabs = ['总览 Dashboard','历史记录','异常事件','Agent 调度记录','设备管理']
const page = ref(0), token = ref(''), connected = ref(false), loading = ref(false), error = ref('')
const devices = ref<Device[]>([]), events = ref<Event[]>([]), tasks = ref<Task[]>([]), summary = ref<Dashboard>()
const device = ref(''), eventType = ref(''), start = ref(''), end = ref(''), cursor = ref<string|null>(null)
const detail = ref<Event|null>(null), related = ref<Event[]>([]), taskBusy = ref(false), notice = ref('')
const types = ['physiological_window','pose_estimation','sleep_posture','audio_event','anomaly_event','agent_decision','cloud_analysis_result','device_metrics']
const format = (value?: string|null) => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '暂无数据'
const filters = computed(() => {
 const p = new URLSearchParams()
 if(device.value) p.set('device_id',device.value)
 if(start.value) p.set('start',new Date(start.value).toISOString())
 if(end.value) p.set('end',new Date(end.value).toISOString())
 const type = page.value===2?'anomaly_event':page.value===3?'agent_decision':eventType.value
 if(type) p.set('event_type',type)
 return p
})
let generation = 0
async function load(more=false) {
 if(!token.value.trim()) {error.value='请输入本地 operator token 后连接';return}
 if(start.value && end.value && start.value >= end.value) {error.value='结束时间必须晚于开始时间';return}
 const current=++generation
 loading.value=true;error.value=''
 try {
  const params = new URLSearchParams(filters.value);params.set('limit','200')
  if(more && cursor.value) params.set('cursor',cursor.value)
  const result = await api<Page<Event>>('/api/v1/events?'+params,token.value)
  const stats = await api<Dashboard>('/api/v1/dashboard?'+filters.value,token.value)
  const ds:Device[]=[];let next:string|null=null
  do {const p:Page<Device>=await api('/api/v1/devices?limit=200'+(next?'&cursor='+encodeURIComponent(next):''),token.value);ds.push(...p.items);next=p.next_cursor} while(next)
  if(current!==generation)return
  events.value=more?[...events.value,...result.items]:result.items;cursor.value=result.next_cursor;summary.value=stats;devices.value=ds;connected.value=true
  await loadTasks()
 } catch(e) {if(current===generation)error.value=String(e)} finally {if(current===generation)loading.value=false}
}
async function loadTasks() {
 const records:Task[]=[];let next:string|null=null
 do {const params=new URLSearchParams({limit:'200'});if(device.value)params.set('device_id',device.value);if(next)params.set('cursor',next)
 const result:Page<Task>=await api('/api/v1/analysis/tasks?'+params,token.value);records.push(...result.items);next=result.next_cursor}while(next)
 tasks.value=records
}
watch(page,()=>{eventType.value='';if(connected.value)void load()})
const samples=computed(()=>events.value.filter(e=>e.event_type==='physiological_window').slice().reverse())
const lineOption=computed(()=>{
 const channels = new Map<string, {name:string;points:[string,number|null][]}>()
 for(const e of samples.value) if(e.event_type==='physiological_window') for(const c of (e.payload.channels??[])){
  const key=`${e.device_id} · ${c.name} (${c.unit??'单位未知'})`
  if(!channels.has(key))channels.set(key,{name:key,points:[]})
  const valid=(c.samples??[]).filter((n):n is number=>n!==null)
  channels.get(key)!.points.push([e.timestamp,valid.length?valid.reduce((a,b)=>a+b,0)/valid.length:null])
 }
 return {color:['#0d9488','#5b75c9','#dc9b4a'],tooltip:{trigger:'axis'},legend:{type:'scroll',bottom:0},grid:{left:60,right:24,top:24,bottom:64},xAxis:{type:'time'},yAxis:{type:'value',scale:true},series:[...channels.values()].map(c=>({name:c.name,type:'line',showSymbol:false,connectNulls:false,data:c.points}))}
})
const sleepOption=computed(()=>{
 const counts:Record<string,number>={}
 for(const e of events.value) if(e.event_type==='sleep_posture'){const label=e.payload.label??'未知';counts[label]=(counts[label]??0)+1}
 return {tooltip:{trigger:'item'},legend:{bottom:0,type:'scroll'},color:['#0d9488','#7cb7b1','#95a9d2','#e1bb78'],series:[{type:'pie',radius:['48%','72%'],center:['50%','45%'],label:{show:false},data:Object.entries(counts).map(([name,value])=>({name,value}))}]}
})
const anomalies=computed(()=>events.value.filter(e=>e.event_type==='anomaly_event'))
async function showDetail(e:Event){detail.value=e;related.value=[];try{
 const ids='related_event_ids' in e.payload?e.payload.related_event_ids:[]
 related.value=await Promise.all((ids??[]).map(id=>api<Event>('/api/v1/events/'+id,token.value)))
}catch(err){error.value=String(err)}}
function decision(e:Event){return e.event_type==='agent_decision'?e.payload.decision:e.event_type==='anomaly_event'?e.payload.description:e.event_type==='cloud_analysis_result'?e.payload.summary:'查看事件详情'}
async function createTask(){
 if(!device.value||!start.value||!end.value){error.value='请先选择设备及分析起止时间';return}
 taskBusy.value=true;notice.value='';error.value=''
 try{const task=await api<Task>('/api/v1/analysis/tasks',token.value,{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({device_id:device.value,start:new Date(start.value).toISOString(),end:new Date(end.value).toISOString(),analysis_type:'multimodal_summary'})});notice.value='MOCK 任务已提交：'+task.task_id;await loadTasks()}catch(e){error.value=String(e)}finally{taskBusy.value=false}
}
async function showResult(task:Task){if(task.result_event_id){try{await showDetail(await api<Event>('/api/v1/events/'+task.result_event_id,token.value))}catch(e){error.value=String(e)}}}
let polling=false
const timer=setInterval(async()=>{if(connected.value&&!polling&&tasks.value.some(t=>['queued','running'].includes(t.status))){polling=true;try{await loadTasks()}catch(e){error.value=String(e)}finally{polling=false}}},2000)
onBeforeUnmount(()=>clearInterval(timer))
function disconnect(){generation++;connected.value=false;token.value='';events.value=[];devices.value=[];tasks.value=[];summary.value=undefined;detail.value=null;related.value=[];notice.value='';error.value='';loading.value=false}
</script>

<template>
 <div class="shell">
  <aside><div class="brand"><span class="brand-icon">✚</span><div>Health Monitor<small>MULTIMODAL · CLOUD</small></div></div><div class="nav-label">工作空间 / WORKSPACE</div>
   <nav><button v-for="(tab,i) in tabs" :key="tab" :class="{active:page===i}" @click="page=i"><span>{{['◫','≋','◇','⌘','▤'][i]}}</span>{{tab}}</button></nav>
   <div class="sidebar-bottom"><span class="dot"></span> 本地开发环境<small>统一协议 v1 · UTC 存储</small></div>
  </aside>
  <main>
   <header><div class="breadcrumb">云端数据平台 <span>/ {{tabs[page]}}</span></div><span class="local-tag">LOCAL MVP</span></header>
   <section class="heading"><div><div class="eyebrow">MULTIMODAL HEALTH INTELLIGENCE</div><h1>{{tabs[page]}}</h1><p>连接多模态记录，追溯每一次观察与决策。</p></div><button class="secondary" :disabled="loading||!connected" @click="load()">↻ 刷新数据</button></section>
   <section class="connection card"><label>操作者认证<input v-model="token" type="password" autocomplete="off" placeholder="粘贴本地 operator token，仅保存在内存"></label><button @click="load()" :disabled="loading">{{loading?'连接中…':'连接 API'}}</button><button v-if="connected" class="secondary" @click="disconnect">断开</button><span :class="['connection-state',connected?'ok':'']">{{connected?'● API 已连接':'○ 等待连接'}}</span></section>
   <div class="mock-banner">MOCK 分析仅用于验证端云闭环，不构成医学判断。事件来源将逐条显示，图表不补齐缺失读数。</div>
   <form class="filters card" @submit.prevent="load()"><label>设备<select v-model="device"><option value="">全部授权设备</option><option v-for="d in devices" :key="d.device_id">{{d.device_id}}</option></select></label><label>开始时间<input type="datetime-local" v-model="start"></label><label>结束时间<input type="datetime-local" v-model="end"></label><label v-if="page<2">事件类型<select v-model="eventType"><option value="">全部事件</option><option v-for="t in types" :key="t">{{t}}</option></select></label><button :disabled="loading||!connected">应用筛选</button><small>界面：本地时间<br>查询：[开始, 结束)</small></form>
   <div v-if="error" class="error" role="alert">{{error}}</div><div v-if="notice" class="notice" role="status">{{notice}}</div><div v-if="loading" class="loading" role="status">正在读取云端数据…</div>
   <template v-if="page===0">
    <div class="stats"><article v-for="(item,i) in [['授权设备',summary?.device_count],['区间事件',summary?.event_count],['异常事件',summary?.anomaly_count],['最近同步',format(summary?.latest_sync)]]" :key="i" class="card stat"><span>{{item[0]}}</span><strong :class="{timestamp:i===3}">{{item[1]??'—'}}</strong><small>{{i===0?'设备注册记录':i===3?'服务器接收时间':'当前筛选范围 · API 统计'}}</small></article></div>
    <div class="chart-grid"><article class="card"><div class="card-title"><h2>生理信号历史趋势</h2><span class="pill">窗口有效点均值</span></div><Chart v-if="samples.length" :option="lineOption"/><div v-else class="empty">暂无生理信号数据</div><p class="chart-note">按设备、通道及单位分组；仅展示已加载窗口，不代表医疗指标。</p></article><article class="card"><div class="card-title"><h2>睡姿分布</h2><span class="pill">已加载记录</span></div><Chart v-if="events.some(e=>e.event_type==='sleep_posture')" :option="sleepOption"/><div v-else class="empty">暂无睡姿数据</div></article></div>
    <article class="card"><div class="card-title"><h2>最近异常事件</h2><button class="text-button" @click="page=2">查看全部 →</button></div><div v-if="!anomalies.length" class="empty compact">暂无已加载异常事件</div><button class="anomaly-row" v-for="e in anomalies.slice(0,5)" :key="e.event_id" @click="showDetail(e)"><span class="severity">异常</span><strong>{{decision(e)}}</strong><span>{{e.device_id}}</span><span>{{format(e.timestamp)}}</span><span class="mock-label" v-if="e.source==='mock'">MOCK</span></button></article>
   </template>
   <template v-if="page===1"><article class="card"><div class="card-title"><h2>历史曲线</h2><span class="pill">生理窗口摘要</span></div><Chart v-if="samples.length" :option="lineOption"/><div v-else class="empty">当前筛选范围暂无可绘制的生理窗口</div></article></template>
   <article v-if="page<4" class="card table-card"><div class="card-title"><h2>{{page===3?'Agent 决策与调度':page===2?'异常事件列表':'多模态事件时间线'}}</h2><span>{{events.length}} 条已加载</span></div><div class="table-wrap"><table><thead><tr><th>事件时间 · 本地</th><th>设备 / 类型</th><th>内容摘要</th><th>模型 / 来源</th><th>置信度</th><th>操作</th></tr></thead><tbody><tr v-for="e in events" :key="e.event_id"><td>{{format(e.timestamp)}}</td><td><strong>{{e.device_id}}</strong><small>{{e.event_type}}</small></td><td class="description">{{decision(e)}}<small v-if="e.event_type==='agent_decision'">{{e.payload.rationale}}<br>动作：{{(e.payload.actions??[]).join('、')||'无'}} · {{e.payload.execution_status}}<br>调度目标：协议未提供</small></td><td>{{e.model_name??'未提供'}}<small :class="{ 'mock-label':e.source==='mock'}">{{e.source==='mock'?'MOCK':e.source}} · {{e.model_version??'版本未知'}}</small></td><td>{{e.confidence==null?'未知':(e.confidence*100).toFixed(1)+'%'}}</td><td><button class="text-button" @click="showDetail(e)">详情 ↗</button></td></tr></tbody></table><div v-if="!events.length" class="empty">{{connected?'当前筛选下暂无事件，请通过 Mock 脚本导入或调整范围。':'连接 API 后查看历史记录。'}}</div></div><div class="table-footer"><span>统计由 API 聚合；曲线、分布与列表基于已加载记录。</span><button class="secondary" v-if="cursor" :disabled="loading" @click="load(true)">加载更多</button></div></article>
   <article v-if="page===2||page===3" class="card"><div class="card-title"><h2>云端 Mock 分析任务</h2><button :disabled="taskBusy||!connected" @click="createTask">创建区间分析</button></div><p class="chart-note">选择设备和起止时间后创建任务。下列状态属于设备任务，不推断为单条异常的分析状态。</p><div class="table-wrap"><table><thead><tr><th>任务 ID</th><th>设备</th><th>状态</th><th>更新时间</th><th>结果</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.task_id"><td>{{task.task_id.slice(0,12)}}…</td><td>{{task.device_id}}</td><td><span class="pill">{{task.status}}</span><small>{{task.error?.message}}</small></td><td>{{format(task.updated_at)}}</td><td><button v-if="task.result_event_id" class="text-button" @click="showResult(task)">查看 MOCK 结果</button><span v-else>—</span></td></tr></tbody></table><div v-if="!tasks.length" class="empty compact">暂无分析任务</div></div></article>
   <article v-if="page===4" class="card"><div class="card-title"><h2>授权设备管理</h2><span class="pill">在线阈值 90 秒 · 同步活动</span></div><div class="table-wrap"><table><thead><tr><th>设备 ID</th><th>状态</th><th>最近在线</th><th>最近同步</th><th>事件数</th><th>操作</th></tr></thead><tbody><tr v-for="d in devices" :key="d.device_id"><td><strong>{{d.device_id}}</strong></td><td>{{d.connectivity}}</td><td>{{format(d.last_seen)}}</td><td>{{format(summary?.devices.find(s=>s.device_id===d.device_id)?.last_sync)}}</td><td>{{summary?.devices.find(s=>s.device_id===d.device_id)?.event_count??0}}</td><td><button class="text-button" @click="device=d.device_id;page=1">查看历史 →</button></td></tr></tbody></table><div v-if="!devices.length" class="empty">暂无已同步的授权设备</div></div><p class="chart-note">设备注册通过离线身份配置，MVP 不提供修改令牌或删除记录的网页操作。</p></article>
   <footer>HEALTH MONITOR / CLOUD MVP <span>数据来源可追溯 · 时间统一 UTC 存储</span></footer>
  </main>
  <div v-if="detail" class="modal-backdrop" @click.self="detail=null"><section class="modal card" role="dialog" aria-modal="true" aria-label="事件详情"><div class="card-title"><h2>事件详情 <span v-if="detail.source==='mock'" class="mock-label">MOCK</span></h2><button class="secondary" @click="detail=null">关闭</button></div><pre>{{JSON.stringify(detail,null,2)}}</pre><h3>关联多模态事件</h3><p v-if="!related.length">无关联记录，或关联事件不可见。</p><button class="anomaly-row" v-for="e in related" :key="e.event_id" @click="showDetail(e)">{{e.event_type}} · {{format(e.timestamp)}} →</button><p v-if="detail.event_type==='agent_decision'" class="chart-note">共享协议仅提供 actions 字符串和 execution_status，未提供结构化工具执行明细；本页不推测调度目标。</p></section></div>
 </div>
</template>
