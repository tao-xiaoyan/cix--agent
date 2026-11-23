<script setup lang="ts">
import { computed, ref } from 'vue'
import ExecutionTrace from './ExecutionTrace.vue'
import { readAgentRun, type useAgentRun, type AgentRun } from './useAgentRun'

const props = defineProps<{ agent: ReturnType<typeof useAgentRun> }>()
const { id, run, unavailable, needsAuth, loading, refresh } = props.agent
const query = ref('')
const pastRun = ref<AgentRun | null>(null)
const lookupError = ref('')
const searching = ref(false)
const displayedRun = computed(() => pastRun.value ?? run.value)
const traceUnavailable = computed(() => !pastRun.value && !!unavailable.value)

async function lookup() {
  const runId = query.value.trim()
  if (!runId || searching.value) return
  searching.value = true
  lookupError.value = ''
  pastRun.value = null
  try {
    pastRun.value = await readAgentRun(runId)
  } catch (error) {
    lookupError.value = (error as Error).message
  } finally {
    searching.value = false
  }
}
</script>

<template>
  <section class="platform-section execution-history" aria-labelledby="history-title">
    <h1 id="history-title">Execution History</h1>
    <form class="history-lookup" @submit.prevent="lookup">
      <label for="history-run-id">Find a run by ID</label>
      <input id="history-run-id" v-model="query" type="text" maxlength="36" spellcheck="false" />
      <button type="submit" :disabled="searching || !query.trim()">{{ searching ? 'Loading…' : 'Find run' }}</button>
      <button v-if="pastRun || lookupError" type="button" @click="pastRun = null; lookupError = ''; query = ''">Show latest run</button>
    </form>
    <p v-if="lookupError" role="status">{{ lookupError }}</p>
    <p v-if="displayedRun" class="run-meta">
      <strong>Run ID</strong> <code>{{ displayedRun.run_id }}</code> · {{ displayedRun.status }}
    </p>
    <p v-if="traceUnavailable" role="status">
      {{ unavailable }} <a v-if="needsAuth" href="#overview">Overview Dashboard</a>
      <button v-if="id" type="button" :disabled="loading" @click="refresh">Retry status</button>
    </p>
    <p v-else-if="!displayedRun" role="status">{{ loading ? 'Loading latest run…' : 'No run selected.' }}</p>
    <ExecutionTrace v-if="displayedRun" :run="displayedRun" :unavailable="traceUnavailable" />
  </section>
</template>
