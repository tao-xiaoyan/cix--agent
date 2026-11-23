<script setup lang="ts">
import { computed } from 'vue'
import AgentConversation from './AgentConversation.vue'
import LiveDepth from './LiveDepth.vue'
import WorkbenchResults from './WorkbenchResults.vue'
import type { useAgentRun } from './useAgentRun'

const props = defineProps<{ active: boolean; authenticated: boolean; agent: ReturnType<typeof useAgentRun> }>()
const { id, run, status, unavailable, needsAuth, loading, ongoing, cancelling, refresh, submit, cancel } = props.agent
const evidence = computed(() => !unavailable.value && run.value?.status === 'completed' ? run.value.evidence : null)
</script>

<template>
  <section class="workbench" aria-labelledby="workbench-title">
    <header class="workbench-heading">
      <div class="scene-copy">
        <h1 id="workbench-title">Sleep Intelligence Demo</h1>
        <div class="workbench-context" aria-label="Scenario capabilities">
          <span class="context-item"><span aria-hidden="true">▣</span> CIX P1</span>
          <span class="context-item"><span aria-hidden="true">✦</span> Qwen Agent</span>
          <span class="context-item"><span aria-hidden="true">◈</span> Gemini 335</span>
        </div>
      </div>
    </header>

    <div class="workbench-grid">
      <LiveDepth :active="active" :authenticated="authenticated" />
      <AgentConversation :run="run" :run-id="id" :status="status" :unavailable="unavailable" :needs-auth="needsAuth"
        :loading="loading" :ongoing="ongoing" :cancelling="cancelling" @submit="submit" @cancel="cancel" @refresh="refresh" />
    </div>
    <WorkbenchResults :evidence="evidence" />
  </section>
</template>
