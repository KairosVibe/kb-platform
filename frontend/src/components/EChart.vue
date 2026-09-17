<script setup lang="ts">
/**
 * ECharts 容器（FRONTEND-SPEC §7.6）
 * 主题切换时销毁重建，确保轴色与网格线跟随暗色令牌；容器尺寸变化自动 resize。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { useUiStore } from '@/stores/ui'

const props = withDefaults(defineProps<{ option: echarts.EChartsOption; height?: string }>(), {
  height: '320px',
})

const el = ref<HTMLDivElement | null>(null)
const ui = useUiStore()
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function render(): void {
  if (!el.value) return
  chart?.dispose()
  chart = echarts.init(el.value, ui.theme === 'dark' ? 'dark' : undefined)
  chart.setOption(props.option)
}

onMounted(() => {
  render()
  if (el.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => chart?.resize())
    observer.observe(el.value)
  }
})

watch(() => props.option, () => chart?.setOption(props.option, true), { deep: true })
watch(() => ui.theme, () => render())

onBeforeUnmount(() => {
  observer?.disconnect()
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="el" class="kb-chart" :style="{ height }" />
</template>

<style scoped>
.kb-chart {
  width: 100%;
  min-height: 180px;
}
</style>
