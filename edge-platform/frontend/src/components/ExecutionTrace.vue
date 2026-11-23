<script setup lang="ts">
import type { AgentRun } from './useAgentRun'

const props = defineProps<{ run: AgentRun | null; unavailable: boolean }>()
const stages = [
  { name: 'Planner', description: 'Interpret the request and plan the next action.' },
  { name: 'Tool Selection', description: 'Select a skill for the input modality.' },
  { name: 'Authorization', description: 'Check the requested input and skill scope.' },
  { name: 'CIX NPU Execution', description: 'Dispatch the authorized skill to the edge.' },
  { name: 'Evidence Validation', description: 'Check output, provenance, and references.' },
  { name: 'Completion', description: 'Return a response grounded in evidence.' },
]

function recorded(index: number) {
  return !props.unavailable && (
    (props.run?.status === 'completed' && !!props.run.evidence) ||
    (index < 2 && !!props.run?.selected_skill)
  )
}
</script>

<template>
  <section class="work-card trace" aria-labelledby="trace-title">
    <div class="card-header">
      <div class="card-title"><span class="card-icon" aria-hidden="true">⇢</span><div><h2 id="trace-title">Execution Trace</h2><small>Agent decision and tool lifecycle</small></div></div>
    </div>
    <ol class="trace-list">
      <li v-for="(stage, index) in stages" :key="stage.name">
        <span class="trace-node" :data-state="recorded(index) ? 'succeeded' : 'idle'" aria-hidden="true">{{ recorded(index) ? '✓' : '·' }}</span>
        <div><strong>{{ stage.name }}</strong><p>{{ index === 1 && recorded(index) ? run?.selected_skill : stage.description }}</p></div>
        <span class="state-label" :data-state="recorded(index) ? 'succeeded' : 'idle'">{{ recorded(index) ? 'Done' : 'Unreported' }}</span>
      </li>
    </ol>
    <div class="trace-footer" role="status">
      <span aria-hidden="true">⌁</span>
      {{ unavailable ? 'Trace unavailable' : run?.status === 'running' ? 'Executing · waiting for server evidence' : 'Stages reflect the server’s recorded evidence.' }}
    </div>
  </section>
</template>
