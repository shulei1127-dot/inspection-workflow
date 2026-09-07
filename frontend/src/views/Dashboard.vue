<template>
  <div class="dashboard-page">
    <div class="page-header">
      <div>
        <h1>数据概览</h1>
        <div class="dashboard-meta">
          数据源：钉钉《客户巡检派单》表 · 按记录时间月份归集
          <span class="muted">｜待派单=需求单号未填写｜待发邮件=未发送且未标记「不涉及」</span>
        </div>
      </div>
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

    <el-alert
      v-if="errorMsg"
      :title="errorMsg"
      type="error"
      show-icon
      :closable="true"
      @close="errorMsg = ''"
      class="dashboard-alert"
    />

    <!-- Stat cards -->
    <div class="stat-row">
      <div class="stat-card kpi-main">
        <div class="label">工单总数（{{ monthLabel }}）</div>
        <div class="value primary">{{ overview.total ?? '-' }}</div>
        <div class="sub">巡检完成 {{ overview.completed ?? '-' }} · 已闭环 {{ overview.closed ?? '-' }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已派单</div>
        <div class="value success">{{ overview.dispatched ?? '-' }}</div>
        <div class="sub">需求单号已回写</div>
      </div>
      <div class="stat-card">
        <div class="label">待派单</div>
        <div class="value warning">{{ overview.pending_dispatch ?? '-' }}</div>
        <div class="sub">其中可立即派单 {{ overview.dispatch_ready ?? '-' }} 条</div>
      </div>
      <div class="stat-card">
        <div class="label">已发邮件</div>
        <div class="value info">{{ overview.emailed ?? '-' }}</div>
        <div class="sub">邮件是否发送 = 是</div>
      </div>
      <div class="stat-card">
        <div class="label">待发邮件</div>
        <div class="value warning">{{ overview.pending_email ?? '-' }}</div>
        <div class="sub">报告已就绪 {{ overview.email_ready ?? '-' }} · 报告未上传 {{ overview.report_missing ?? '-' }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已闭环</div>
        <div class="value success">{{ overview.closed ?? '-' }}</div>
        <div class="sub">工单是否闭环 = 是</div>
      </div>
    </div>

    <!-- Charts -->
    <div class="charts-grid">
      <div class="chart-card">
        <h3>派单状态</h3>
        <div ref="dispatchChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card">
        <h3>邮件状态</h3>
        <div ref="emailChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card chart-full">
        <h3>区域分布</h3>
        <div ref="regionChartRef" class="chart-container"></div>
      </div>
      <div class="chart-card chart-full">
        <h3>月度趋势（近 6 个月）</h3>
        <div ref="trendChartRef" class="chart-container"></div>
      </div>
    </div>
  </div>

</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from 'echarts'
import { getOverview, getByRegion, getByStatus, getMonthlyTrend, createEventStream } from '../api'

const selectedMonth = ref<string>('')
const overview = ref<Record<string, any>>({})
const errorMsg = ref('')

const regionChartRef = ref<HTMLElement>()
const dispatchChartRef = ref<HTMLElement>()
const emailChartRef = ref<HTMLElement>()
const trendChartRef = ref<HTMLElement>()

const chartInstances = new Map<string, echarts.ECharts>()
let ws: WebSocket | null = null

const monthLabel = computed(() => {
  const [y, m] = (selectedMonth.value || currentMonth()).split('-')
  return `${y}年${Number(m)}月`
})

function currentMonth() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function mountChart(key: string, el: HTMLElement | undefined): echarts.ECharts | null {
  if (!el) return null
  const old = chartInstances.get(key)
  if (old) old.dispose()
  const chart = echarts.init(el)
  chartInstances.set(key, chart)
  return chart
}

function emptyOption(): echarts.EChartsOption {
  return {
    title: {
      text: '暂无数据',
      left: 'center',
      top: 'middle',
      textStyle: { color: '#bfbfbf', fontSize: 14, fontWeight: 'normal' },
    },
  }
}


function renderRegionChart(items: any[]) {
  const chart = mountChart('region', regionChartRef.value)
  if (!chart) return
  if (!items.length) {
    chart.setOption(emptyOption())
    return
  }
  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 110, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'value', minInterval: 1 },
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
      label: { show: true, position: 'right', fontSize: 11, color: '#666' },
    }],
  })
}

function renderPieChart(
  key: string,
  el: HTMLElement | undefined,
  items: any[],
  colorMap: Record<string, string>,
) {
  const chart = mountChart(key, el)
  if (!chart) return
  if (!items.length) {
    chart.setOption(emptyOption())
    return
  }
  chart.setOption({
    tooltip: { trigger: 'item', formatter: '{b}：{c} 条（{d}%）' },
    legend: { bottom: 0, type: 'scroll' },
    color: items.map((i: any) => colorMap[i.status] || '#909399'),
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '44%'],
      data: items.map((i: any) => ({ name: i.status, value: i.count })),
      label: {
        show: true,
        formatter: '{b}\n{c} 条 · {d}%',
        fontSize: 11,
        lineHeight: 15,
      },
      labelLine: { length: 8, length2: 6 },
      emphasis: { label: { show: true, fontWeight: 'bold' } },
    }],
  })
}


function renderTrendChart(items: any[]) {
  const chart = mountChart('trend', trendChartRef.value)
  if (!chart) return
  const months = items.map((i: any) => (i.month || '').slice(5) + '月')
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 50, right: 20, top: 20, bottom: 40 },
    xAxis: {
      type: 'category',
      data: months,
      boundaryGap: true,
    },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      {
        name: '工单总数',
        type: 'bar',
        barWidth: 26,
        itemStyle: { color: '#409eff', borderRadius: [3, 3, 0, 0] },
        data: items.map((i: any) => i.total || 0),
      },
      {
        name: '已闭环',
        type: 'line',
        smooth: true,
        itemStyle: { color: '#52c41a' },
        lineStyle: { width: 2 },
        data: items.map((i: any) => i.closed || 0),
      },
    ],
  })
}

async function loadAll() {
  const month = selectedMonth.value || currentMonth()
  errorMsg.value = ''
  let failures = 0

  try {
    overview.value = await getOverview(month)
  } catch (e: any) {
    failures += 1
    console.error('overview failed', e)
  }
  try {
    const region: any = await getByRegion(month)
    renderRegionChart(region.items || [])
  } catch (e: any) {
    failures += 1
    console.error('by-region failed', e)
  }
  try {
    const status: any = await getByStatus(month)
    renderDispatchChart(status.dispatch_status || [])
    renderEmailChart(status.email_status || [])
  } catch (e: any) {
    failures += 1
    console.error('by-status failed', e)
  }
  try {
    const trend: any = await getMonthlyTrend(month)
    renderTrendChart(trend.items || [])
  } catch (e: any) {
    failures += 1
    console.error('monthly-trend failed', e)
  }

  if (failures > 0) {
    errorMsg.value = '部分数据加载失败，请稍后刷新重试'
  }
}

function renderDispatchChart(items: any[]) {
  renderPieChart('dispatch', dispatchChartRef.value, items, {
    '已派单': '#52c41a',
    '待派单': '#faad14',
  })
}

function renderEmailChart(items: any[]) {
  renderPieChart('email', emailChartRef.value, items, {
    '已发送': '#52c41a',
    '待发送': '#409eff',
    '报告未上传': '#faad14',
    '不涉及': '#d9d9d9',
  })
}

function handleResize() {
  chartInstances.forEach(c => c.resize())
}

onMounted(() => {
  loadAll()
  window.addEventListener('resize', handleResize)
  // Auto-refresh when work order / dispatch / email state changes
  ws = createEventStream((type, _data) => {
    if (type === 'work_order.closure_updated' || type === 'monitor.poll.completed' || type === 'monitor.dispatch_poll.completed') {
      loadAll()
    }
  })
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (ws) ws.close()
  chartInstances.forEach(c => c.dispose())
  chartInstances.clear()
})
</script>

<style scoped>
.dashboard-page {
  min-width: 960px;
}

.page-header {
  align-items: flex-start;
}

.dashboard-meta {
  margin-top: 6px;
  font-size: 12px;
  color: #8c8c8c;
}

.dashboard-meta .muted {
  color: #bfbfbf;
}

.dashboard-alert {
  margin-bottom: 16px;
}

.stat-card .value.primary {
  color: #409eff;
}

.kpi-main .value {
  font-size: 34px;
}

.stat-card .sub {
  margin-top: 6px;
  font-size: 12px;
  color: #8c8c8c;
  line-height: 1.5;
}
</style>
