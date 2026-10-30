<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
const props = defineProps<{ option: echarts.EChartsCoreOption }>()
const container = ref<HTMLDivElement>()
let chart: echarts.ECharts | undefined
let observer: ResizeObserver | undefined
onMounted(() => { chart = echarts.init(container.value); chart.setOption(props.option); observer = new ResizeObserver(() => chart?.resize()); observer.observe(container.value!) })
watch(() => props.option, value => chart?.setOption(value, true), {deep:true})
onBeforeUnmount(() => {observer?.disconnect(); chart?.dispose()})
</script>
<template><div ref="container" class="chart" role="img" aria-label="历史数据图表"></div></template>
