<script setup lang="ts">
import type { DepthEvidence } from './useAgentRun'

defineProps<{ evidence: DepthEvidence | null }>()
</script>

<template>
  <section class="work-card results" aria-labelledby="results-title">
    <div class="card-header">
      <div class="card-title"><span class="card-icon" aria-hidden="true">▤</span><div><h2 id="results-title">Result &amp; Evidence</h2><small>Authorized historical SLP sample analysis</small></div></div>
      <span v-if="evidence" class="state-label" data-state="succeeded">Verified</span>
    </div>
    <p v-if="!evidence" class="results-message" role="status">Verified depth result unavailable.</p>
    <div class="result-grid">
      <article class="skill-result">
        <h3><span aria-hidden="true">◈</span> Depth posture</h3>
        <template v-if="evidence">
          <p class="prediction">{{ evidence.predicted_posture }}</p>
          <dl>
            <div><dt>Dataset</dt><dd><code>{{ evidence.dataset_id }}</code></dd></div>
            <div><dt>Subject / pose</dt><dd>{{ evidence.subject_id }} / {{ evidence.pose_id }}</dd></div>
            <div><dt>Model version</dt><dd>{{ evidence.model_version }}</dd></div>
            <div><dt>Inference device</dt><dd>{{ evidence.inference_device }}</dd></div>
            <div><dt>Execution status</dt><dd>{{ evidence.execution_status }}</dd></div>
            <div><dt>Execution ID</dt><dd><code>{{ evidence.execution_id }}</code></dd></div>
            <div><dt>Evidence reference</dt><dd><code>{{ evidence.evidence_reference }}</code></dd></div>
          </dl>
          <details class="result-details">
            <summary>SHA-256 provenance</summary>
            <dl>
              <div><dt>Input SHA-256</dt><dd><code>{{ evidence.input_sha256 }}</code></dd></div>
              <div><dt>Model SHA-256</dt><dd><code>{{ evidence.model_sha256 }}</code></dd></div>
            </dl>
          </details>
        </template>
      </article>
    </div>
  </section>
</template>
