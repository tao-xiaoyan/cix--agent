<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { AgentRun } from './useAgentRun'

const props = defineProps<{
  run: AgentRun | null
  runId: string | null
  status: string
  unavailable: string
  needsAuth: boolean
  loading: boolean
  ongoing: boolean
  cancelling: boolean
}>()
const emit = defineEmits<{ submit: [string]; cancel: []; refresh: [] }>()
const example = '请分析受试者 101 的第 8 个深度姿态样本，告诉我是什么睡姿，并提供推理依据。'
const draft = ref('')
const submittedQuery = ref('')
const messageLog = ref<HTMLElement | null>(null)

function submitMessage() {
  const query = draft.value.trim()
  if (!query || props.ongoing || props.loading || props.cancelling) return
  submittedQuery.value = query
  emit('submit', query)
}

watch([() => props.run, () => props.unavailable], async () => {
  await nextTick()
  if (messageLog.value) messageLog.value.scrollTop = messageLog.value.scrollHeight
})
</script>

<template>
  <section class="work-card conversation" aria-labelledby="conversation-title">
    <div class="card-header">
      <div class="card-title"><span class="card-icon" aria-hidden="true">✦</span><div><h2 id="conversation-title">Agent Conversation</h2><small>Qwen3-4B · local agent</small></div></div>
      <span class="state-label" :data-state="status" role="status">{{ status === 'idle' ? 'Ready' : status }}</span>
    </div>
    <div ref="messageLog" class="message-log" role="log" aria-label="Conversation history" tabindex="0">
      <div v-if="runId" class="run-meta"><strong>Run ID</strong> <code>{{ runId }}</code></div>
      <div class="intro-message">
        <span class="message-icon" aria-hidden="true">✦</span>
        <div><p>Ask Qwen to analyze the authorized historical SLP sample for subject 101, pose 8.</p></div>
      </div>
      <div v-if="run?.query || submittedQuery" class="user-message"><strong>You</strong><p>{{ run?.query || submittedQuery }}</p></div>
      <div v-if="run?.answer && !unavailable" class="intro-message follow-up">
        <span class="message-icon" aria-hidden="true">✦</span>
        <div><strong>Qwen Agent · evidence report</strong><p lang="zh-CN">{{ run.answer }}</p></div>
      </div>
      <p v-if="unavailable" class="run-notice" role="status">
        {{ unavailable }} <a v-if="needsAuth" href="#overview">Overview Dashboard</a>
      </p>
      <p v-else-if="run?.error" class="run-notice" role="status">Execution failed · {{ run.error }}</p>
      <p v-else-if="ongoing && !run" class="run-notice" role="status">Waiting for the edge service to confirm the run…</p>
    </div>
    <form class="composer" @submit.prevent="submitMessage">
      <div class="examples" aria-label="Example queries">
        <span>Example query</span>
        <div class="example-buttons">
          <button type="button" @click="draft = example"><span aria-hidden="true">◈</span> Historical SLP sample · 101 / 8</button>
        </div>
      </div>
      <textarea v-model="draft" rows="2" maxlength="1000" aria-label="Analysis request" placeholder="Ask about the authorized depth sample…" />
      <div class="composer-actions">
        <button v-if="runId && unavailable" type="button" :disabled="loading || cancelling" @click="emit('refresh')">Retry status</button>
        <div class="run-actions">
          <button type="button" class="cancel-button" :disabled="!run || !ongoing || loading || cancelling || needsAuth" @click="emit('cancel')">Cancel task</button>
          <button type="submit" class="run-button" :disabled="!draft.trim() || ongoing || loading || cancelling">Run on device <span aria-hidden="true">↑</span></button>
        </div>
      </div>
    </form>
  </section>
</template>
