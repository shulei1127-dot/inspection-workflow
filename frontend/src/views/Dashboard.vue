<template>
  <div class="dashboard-page">
    <div class="page-header">
      <h1>数据概览</h1>
      <el-date-picker
        v-model="selectedMonth"
        type="month"
        placeholder="选择月份"
        format="YYYY-MM"
        value-format="YYYY-MM"
        @change="loadAll"
        size="default"
      />
    </div>

    <!-- Stat cards -->
    <div class="stat-row" v-loading="loading">
      <div class="stat-card">
        <div class="label">工单总数</div>
        <div class="value">{{ overview.total || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已派单</div>
        <div class="value success">{{ overview.dispatched || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已发邮件</div>
        <div class="value info">{{ overview.emailed || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">待派单</div>
        <div class="value warning">{{ overview.pending_dispatch || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">待发邮件</div>
        <div class="value warning">{{ overview.pending_email || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">派单失败</div>
        <div class="value danger">{{ overview.dispatch_failed || 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="label">邮件失败</div>
        <div class="value danger">{{ overview.email_failed || 0 }}</div>
      </div>
    </div>

    <!-- Charts -->
    <div class="charts-grid">
      <div class="chart-card">
        <h3>区域分布</h3>
        <div ref="regionChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3>类型分布</h3>
        <div ref="typeChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3>派单状态</h3>
        <div ref="dispatchChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3>邮件状态</h3>
        <div ref="emailChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card chart-full">
        <h3>月度趋势</h3>
        <div ref="trendChartRef" class="chart-container"></div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts'
import { getOverview, getByRegion, getByType, getByStatus, getMonthlyTrend, getDispatchPending, getEmailPending, createEventStream } from '../api'

const selectedMonth = ref<string>('')
const overview = ref<Record<string, any>>({})
const loading = ref(false)

const regionChartRef = ref<HTMLElement>()
const typeChartRef = ref<HTMLElement>()
const dispatchChartRef = ref<HTMLElement>()
const emailChartRef = ref<HTMLElement>()
const trendChartRef = ref<HTMLElement>()

let charts: echarts.ECharts[] = []
let ws: WebSocket | null = null

function currentMonth() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

async function loadAll() {
  loading.value = true
  const month = selectedMonth.value || currentMonth()

  const [ov, region, type, status, trend, dispatchRes, emailRes] = await Promise.all([
    getOverview(month).catch(() => ({})),
    getByRegion(month).catch(() => ({ items: [] })),
    getByType(month).catch(() => ({ items: [] })),
    getByStatus(month).catch(() => ({ dispatch_status: [], email_status: [] })),
    getMonthlyTrend(month).catch(() => ({ items: [] })),
    getDispatchPending().catch(() => ({ total: 0 })),
    getEmailPending().catch(() => ({ total: 0 })),
  ])

  overview.value = ov
  // Use AITable real-time counts for pending dispatch/email (consistent with Monitor page)
  overview.value.pending_dispatch = dispatchRes.total ?? 0
  overview.value.pending_email = emailRes.total ?? 0

  await nextTick()

  renderRegionChart(region.items || [])
  renderTypeChart(type.items || [])
  renderDispatchChart(status.dispatch_status || [])
  renderEmailChart(status.email_status || [])
  renderTrendChart(trend.items || [])
  loading.value = false
}

function renderRegionChart(items: any[]) {
  if (!regionChartRef.value) return
  const chart = echarts.init(regionChartRef.value)
  charts.push(chart)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 100, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'value' },
    yAxis: {
      type: 'category',
      data: items.map((i: any) => i.region).reverse(),
      axisLabel: { fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: items.map((i: any) => i.count).reverse(),
      itemStyle: { color: '#409eff', borderRadius: [0, 4, 4, 0] },
      barWidth: 18,
    }],
  })
}

function renderTypeChart(items: any[]) {
  if (!typeChartRef.value) return
  const chart = echarts.init(typeChartRef.value)
  charts.push(chart)
  const colors = ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399', '#9b59b6', '#1abc9c']
  chart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, type: 'scroll' },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['50%', '45%'],
      data: items.map((i: any, idx: number) => ({
        name: i.type,
        value: i.count,
        itemStyle: { color: colors[idx % colors.length] },
      })),
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: 'bold' } },
    }],
  })
}

const STATUS_COLORS: Record<string, string> = {
  '已派单': '#67c23a',
  '待派单': '#e6a23c',
  '派单失败': '#f56c6c',
}

function renderDispatchChart(items: any[]) {
  if (!dispatchChartRef.value) return
  const chart = echarts.init(dispatchChartRef.value)
  charts.push(chart)
  chart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['50%', '45%'],
      data: items.map((i: any) => ({
        name: i.status,
        value: i.count,
        itemStyle: { color: STATUS_COLORS[i.status] || '#909399' },
      })),
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: 'bold' } },
    }],
  })
}

const EMAIL_COLORS: Record<string, string> = {
  '已发送': '#67c23a',
  '待发送': '#e6a23c',
  '发送失败': '#f56c6c',
}

function renderEmailChart(items: any[]) {
  if (!emailChartRef.value) return
  const chart = echarts.init(emailChartRef.value)
  charts.push(chart)
  chart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['50%', '45%'],
      data: items.map((i: any) => ({
        name: i.status,
        value: i.count,
        itemStyle: { color: EMAIL_COLORS[i.status] || '#909399' },
      })),
      label: { show: false },
      emphasis: { label: { show: true, fontWeight: 'bold' } },
    }],
  })
}

function renderTrendChart(items: any[]) {
  if (!trendChartRef.value) return
  const chart = echarts.init(trendChartRef.value)
  charts.push(chart)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: items.map((i: any) => i.date?.slice(5) || ''),
      boundaryGap: false,
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [{
      type: 'line',
      data: items.map((i: any) => i.count),
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#409eff' },
      lineStyle: { width: 2 },
    }],
  })
}

function handleResize() {
  charts.forEach(c => c.resize())
}

onMounted(() => {
  loadAll()
  window.addEventListener('resize', handleResize)
  // Auto-refresh when work order status changes
  ws = createEventStream((type, _data) => {
    if (type === 'work_order.closure_updated' || type === 'monitor.poll.completed' || type === 'monitor.dispatch_poll.completed') {
      loadAll()
    }
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (ws) ws.close()
  charts.forEach(c => c.dispose())
  charts = []
})
</script>
