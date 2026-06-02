<template>
  <div class="work-orders-page">
    <div class="page-header">
      <h1>工单列表</h1>
    </div>

    <!-- Filters -->
    <div class="card filter-bar">
      <el-form :inline="true" @submit.prevent="loadData">
        <el-form-item label="月份">
          <el-date-picker
            v-model="filters.month"
            type="month"
            placeholder="选择月份"
            format="YYYY-MM"
            value-format="YYYY-MM"
            clearable
            size="default"
          />
        </el-form-item>
        <el-form-item label="工单类型">
          <el-select v-model="filters.order_type" placeholder="全部" clearable size="default">
            <el-option label="产品巡检" value="产品巡检" />
            <el-option label="日志分析" value="日志分析" />
          </el-select>
        </el-form-item>
        <el-form-item label="闭环状态">
          <el-select v-model="filters.closure_status" placeholder="全部" clearable size="default">
            <el-option label="已闭环" value="已闭环" />
            <el-option label="未闭环" value="未闭环" />
            <el-option label="闭环中" value="闭环中" />
            <el-option label="闭环失败" value="闭环失败" />
          </el-select>
        </el-form-item>
        <el-form-item label="推送状态">
          <el-select v-model="filters.dt_sync_status" placeholder="全部" clearable size="default">
            <el-option label="已推送" value="synced" />
            <el-option label="未推送" value="pending" />
            <el-option label="推送失败" value="failed" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadData" size="default">查询</el-button>
          <el-button @click="resetFilters" size="default">重置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- Action bar -->
    <div class="card action-bar">
      <div class="action-left">
        <span class="total-label">共 {{ total }} 条工单</span>
        <span class="closed-label">| 已闭环 {{ closedCount }} 条</span>
        <span class="unclosed-label">| 未闭环 {{ total - closedCount }} 条</span>
        <span v-if="pendingCount > 0" class="pending-label">（{{ pendingCount }} 条未推送）</span>
      </div>
      <div class="action-right">
        <el-button
          type="primary"
          :loading="fetching"
          @click="handleFetch"
        >
          {{ fetching ? '拉取中...' : '拉取工单' }}
        </el-button>
        <el-button
          type="success"
          :loading="pushing"
          :disabled="pendingCount === 0"
          @click="handlePush"
        >
          {{ pushing ? '推送中...' : `推送到钉钉${pendingCount > 0 ? '(' + pendingCount + ')' : ''}` }}
        </el-button>
        <el-button
          type="success"
          :loading="batchPushing"
          :disabled="selectedItems.length === 0"
          @click="handleBatchPush"
        >
          {{ batchPushing ? '推送中...' : `推送选中(${selectedItems.length})` }}
        </el-button>
        <el-button
          type="warning"
          :loading="syncing"
          @click="handleSyncClosureStatus"
        >
          {{ syncing ? '同步中...' : '同步闭环状态' }}
        </el-button>
        <el-button
          type="info"
          :loading="adjusting"
          :disabled="selectedItems.length === 0"
          @click="handleAdjustSelected"
        >
          {{ adjusting ? '调整中...' : `调整到月末(${selectedItems.length})` }}
        </el-button>
        <el-button
          type="info"
          :loading="adjustingAll"
          :disabled="!filters.month"
          @click="handleAdjustAll"
        >
          {{ adjustingAll ? '调整中...' : '当月全部调整到月末' }}
        </el-button>
      </div>
    </div>

    <!-- Table -->
    <div class="card">
      <el-table
        :data="items"
        stripe
        style="width: 100%"
        v-loading="loading"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="55" />
        <el-table-column prop="pts_order_id" label="工单号" width="100" show-overflow-tooltip />
        <el-table-column label="链接" width="60" align="center">
          <template #default="{ row }">
            <a v-if="row.pts_order_url" :href="row.pts_order_url" target="_blank" class="link">打开</a>
          </template>
        </el-table-column>
        <el-table-column prop="customer_name" label="客户" min-width="140" show-overflow-tooltip />
        <el-table-column prop="product_name" label="产品" min-width="120" show-overflow-tooltip />
        <el-table-column prop="order_type" label="类型" width="100" show-overflow-tooltip />
        <el-table-column prop="region" label="区域" width="110" show-overflow-tooltip />
        <el-table-column prop="assigner_name" label="交付负责人" width="100" show-overflow-tooltip />
        <el-table-column label="工单计划完成时间" width="160">
          <template #default="{ row }">
            {{ row.planned_completion }}
            <el-tag v-if="row.planned_completion_adjusted" type="info" size="small" style="margin-left: 4px">已调整</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="closure_status" label="闭环状态" width="120">
          <template #default="{ row }">
            <el-tag
              :type="closureTagType(row.closure_status)"
            >
              {{ row.closure_status || '未闭环' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="推送" width="80" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.dt_sync_status === 'synced'" type="success" size="small">已推送</el-tag>
            <el-tag v-else-if="row.dt_sync_status === 'failed'" type="danger" size="small">失败</el-tag>
            <el-tag v-else type="warning" size="small">未推送</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="loadData"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getWorkOrders, runSync, pushToDingtalk, syncClosureStatus, createEventStream, batchPushToDingtalk, adjustPlannedCompletion } from '../api'

const loading = ref(false)
const fetching = ref(false)
const pushing = ref(false)
const batchPushing = ref(false)
const syncing = ref(false)
const adjusting = ref(false)
const adjustingAll = ref(false)
let ws: WebSocket | null = null
const items = ref<any[]>([])
const selectedItems = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 50

const filters = ref({
  month: '',
  order_type: '',
  dt_sync_status: '',
  closure_status: '',
})

const pendingCount = computed(() => items.value.filter(i => i.dt_sync_status !== 'synced').length)
const closedCount = computed(() => items.value.filter(i => i.closure_status === '已闭环').length)

function closureTagType(status: string) {
  switch (status) {
    case '已闭环': return 'success'
    case '闭环中': return ''
    case '闭环失败': return 'danger'
    default: return 'warning'  // 未闭环
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await getWorkOrders({
      month: filters.value.month || undefined,
      order_type: filters.value.order_type || undefined,
      dt_sync_status: filters.value.dt_sync_status || undefined,
      closure_status: filters.value.closure_status || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    items.value = res.items || []
    total.value = res.total || 0
  } catch {
    // silent
  } finally {
    loading.value = false
  }
}

async function handleFetch() {
  fetching.value = true
  try {
    const month = filters.value.month || undefined
    const res = await runSync(month)
    const adj = res.adjust_result || {}
    const msg = `拉取完成: 新增${res.created_count || 0} 更新${res.updated_count || 0} 共${res.fetched_count || 0}条` +
      (adj.adjusted ? ` | 月末调整: ${adj.adjusted}条` : '') +
      (adj.pts_updated ? ` | PTS同步: ${adj.pts_updated}条` : '')
    ElMessage.success(msg)
    loadData()
  } catch (e: any) {
    ElMessage.error('拉取失败: ' + e.message)
  } finally {
    fetching.value = false
  }
}

async function handlePush() {
  pushing.value = true
  try {
    const month = filters.value.month || undefined
    const res = await pushToDingtalk(month)
    ElMessage.success(`推送完成: 成功${res.pushed || 0} 失败${res.failed || 0}`)
    loadData()
  } catch (e: any) {
    ElMessage.error('推送失败: ' + e.message)
  } finally {
    pushing.value = false
  }
}

function handleSelectionChange(selection: any[]) {
  selectedItems.value = selection
}

async function handleBatchPush() {
  if (selectedItems.value.length === 0) {
    ElMessage.warning('请先选择要推送的工单')
    return
  }

  batchPushing.value = true
  try {
    const ids = selectedItems.value.map(item => item.id)
    const res = await batchPushToDingtalk({ work_order_ids: ids })
    ElMessage.success(`批量推送完成: 成功${res.pushed || 0} 失败${res.failed || 0}`)
    selectedItems.value = []
    loadData()
  } catch (e: any) {
    ElMessage.error('批量推送失败: ' + e.message)
  } finally {
    batchPushing.value = false
  }
}

async function handleSyncClosureStatus() {
  syncing.value = true
  try {
    const res = await syncClosureStatus()
    ElMessage.success(`同步完成: 检查${res.checked || 0}条 更新${res.updated || 0}条 失败${res.failed || 0}条`)
    loadData()
  } catch (e: any) {
    ElMessage.error('同步失败: ' + e.message)
  } finally {
    syncing.value = false
  }
}

async function handleAdjustSelected() {
  if (selectedItems.value.length === 0) {
    ElMessage.warning('请先选择要调整的工单')
    return
  }
  adjusting.value = true
  try {
    const ids = selectedItems.value.map(item => item.id)
    const res = await adjustPlannedCompletion({ work_order_ids: ids })
    ElMessage.success(`调整完成: 已调整${res.adjusted || 0}条 跳过${res.skipped || 0}条`)
    selectedItems.value = []
    loadData()
  } catch (e: any) {
    ElMessage.error('调整失败: ' + e.message)
  } finally {
    adjusting.value = false
  }
}

async function handleAdjustAll() {
  const month = filters.value.month
  if (!month) {
    ElMessage.warning('请先选择月份')
    return
  }
  adjustingAll.value = true
  try {
    const res = await adjustPlannedCompletion({ month })
    ElMessage.success(`调整完成: 已调整${res.adjusted || 0}条 跳过${res.skipped || 0}条`)
    loadData()
  } catch (e: any) {
    ElMessage.error('调整失败: ' + e.message)
  } finally {
    adjustingAll.value = false
  }
}

function resetFilters() {
  filters.value = { month: '', order_type: '', dt_sync_status: '', closure_status: '' }
  page.value = 1
  loadData()
}

onMounted(() => {
  loadData()
  // Listen for work order closure updates from WebSocket
  ws = createEventStream((type, data) => {
    if (type === 'work_order.closure_updated') {
      // Update the item in-place if it's in the current list
      const item = items.value.find(i => i.pts_order_id === data.pts_order_id)
      if (item) {
        item.closure_status = data.closure_status
      }
    }
  })
})

onUnmounted(() => {
  if (ws) ws.close()
})
</script>

<style scoped>
.filter-bar {
  margin-bottom: 8px;
}
.filter-bar :deep(.el-form-item) {
  margin-bottom: 0;
}
.action-bar {
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
}
.action-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.total-label {
  font-size: 13px;
  color: #666;
}
.closed-label {
  font-size: 13px;
  color: #67c23a;
}
.unclosed-label {
  font-size: 13px;
  color: #e6a23c;
}
.pending-label {
  font-size: 13px;
  color: #e6a23c;
}
.action-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
.link {
  color: #409eff;
  text-decoration: none;
  font-size: 12px;
}
.link:hover {
  text-decoration: underline;
}
</style>
