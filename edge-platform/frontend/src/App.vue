<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import LiveDashboard from './components/LiveDashboard.vue'
import SleepWorkbench from './components/SleepWorkbench.vue'
import ExecutionHistory from './components/ExecutionHistory.vue'
import { useAgentRun } from './components/useAgentRun'
import { skills } from './components/workbenchSkills'

const pages = [
  { id: 'agent', title: 'Workbench', icon: '✦' },
  { id: 'skills', title: 'Models & Skills', icon: '◇' },
  { id: 'runs', title: 'Execution History', icon: '≡' },
  { id: 'devices', title: 'Device Status', icon: '▣', description: 'Open Overview Dashboard for the existing edge connection and device telemetry.' },
  { id: 'overview', title: 'Overview Dashboard', icon: '▦' },
  { id: 'settings', title: 'Settings', icon: '⚙' },
] as const

function readPage() {
  const id = window.location.hash.slice(1)
  if (id === 'models') return 'skills'
  return pages.find(page => page.id === id)?.id ?? 'agent'
}

const activePage = ref(readPage())
const agent = useAgentRun(computed(() => activePage.value === 'agent' || activePage.value === 'runs'))
const authenticated = ref(false)
const currentPage = computed(() => pages.find(page => page.id === activePage.value)!)
const connection = ref('连接中')
const connected = computed(() => connection.value === '实时连接')
function syncPage() {
  activePage.value = readPage()
  window.scrollTo(0, 0)
}
onMounted(() => window.addEventListener('hashchange', syncPage))
onUnmounted(() => window.removeEventListener('hashchange', syncPage))
</script>

<template>
  <header class="platform-header" lang="en">
    <a href="#agent" class="platform-brand">
      <span class="platform-logo" aria-hidden="true">✦</span>
      <span><strong>CIX AI Edge Platform</strong><small>Developer Console</small></span>
    </a>
    <div class="platform-actions">
      <span class="connection-status" :class="{ connected }" role="status" lang="zh-CN">
        <i aria-hidden="true"></i><span>端侧 · {{ connection }}</span>
      </span>
      <a href="#settings" class="settings-link" :aria-current="activePage === 'settings' ? 'page' : undefined" aria-label="Settings">
        <span aria-hidden="true">⚙</span>
      </a>
    </div>
  </header>

  <div class="platform-layout">
    <aside class="platform-sidebar" lang="en">
      <nav aria-label="Platform navigation">
        <p class="nav-group">WORKSPACE</p>
        <a v-for="page in pages.slice(0, 4)" :key="page.id" :href="'#' + page.id"
          :aria-current="activePage === page.id ? 'page' : undefined">
          <span class="nav-icon" aria-hidden="true">{{ page.icon }}</span>{{ page.title }}
        </a>
        <p class="nav-group system-group">MONITORING</p>
        <a href="#overview" :aria-current="activePage === 'overview' ? 'page' : undefined">
          <span class="nav-icon" aria-hidden="true">▦</span>Overview Dashboard
        </a>
      </nav>
    </aside>

    <main class="platform-main">
      <LiveDashboard v-show="activePage === 'overview'" v-model:authenticated="authenticated"
        :active="activePage === 'overview'" @connection="connection = $event" />
      <SleepWorkbench v-show="activePage === 'agent'" :active="activePage === 'agent'"
        :authenticated="authenticated" :agent="agent" lang="en" />
      <ExecutionHistory v-if="activePage === 'runs'" :agent="agent" lang="en" />
      <section v-else-if="activePage !== 'overview' && activePage !== 'agent'" class="work-card platform-section" lang="en">
        <h1>{{ currentPage.title }}</h1>
        <template v-if="activePage === 'skills'">
          <p>Qwen3-4B selects from these validated edge skills. This catalog describes capabilities and compute placement.</p>
          <div class="skill-catalog">
            <article v-for="skill in skills" :key="skill.id" class="skill-result">
              <h2><span aria-hidden="true">{{ skill.icon }}</span> {{ skill.title }}</h2>
              <p><code>{{ skill.id }}</code></p>
              <dl>
                <div><dt>Input modality</dt><dd>{{ skill.modality }}</dd></div>
                <div><dt>Compute location</dt><dd>On-device · CIX P1 NPU</dd></div>
              </dl>
            </article>
          </div>
        </template>
        <template v-else-if="activePage === 'settings'">
          <p lang="zh-CN">端侧连接状态：{{ connection }}</p>
          <a href="#overview">Open Overview Dashboard</a>
        </template>
        <template v-else-if="'description' in currentPage">
          <p>{{ currentPage.description }}</p>
          <a href="#overview">Open Overview Dashboard</a>
        </template>
      </section>
    </main>
  </div>
</template>
