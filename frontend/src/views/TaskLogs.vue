<template>
  <div class="task-log-page">
    <div class="page-header">
      <h1>任务日志</h1>
    </div>

    <div class="card">
      <div class="card-header">
        <h3>全部任务执行记录</h3>
        <div style="display: flex; gap: 8px; align-items: center">
          <el-radio-group v-model="filterType" size="small" @change="handleFilterChange">
            <el-radio-button label="">全部</el-radio-button>
            <el-radio-button label="sync_full">PTS完整同步</el-radio-button>
            <el-radio-button label="sync_fetch">PTS拉取</el-radio-button>
            <el-radio-button label="sync_push">AITable推送</el-radio-button>
            <el-radio-button label="dispatch">云集派单</el-radio-button>
            <el-radio-button label="email">巡检邮件</el-radio-button>
            <el-radio-button label="closure">工单闭环</el-radio-button>
            <el-radio-button label="email_pre_analysis">邮件预分析</el-radio-button>
          </el-radio-group>
          <el-button size="small" @click="loadData">刷新</el-button>
        </div>
      </div>
      <el-table :data="items" stripe size="small" v-loading="loading">
        <el-table-column prop="time" label="时间" width="170" />
        <el-table-column prop="task_type" label="任务类型" width="130">
          <template #default="{ row }">
            <el-tag :type="taskTagType(row.task_type)" size="small">{{ row.task_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="trigger_source" label="触发" width="70">
          <template #default="{ row }">
            <el-tag :type="row.trigger_source === '定时' ? 'info' : 'primary'" size="small">{{ row.trigger_source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="summary" label="摘要" min-width="250" show-overflow-tooltip />
        <el-table-column prop="error" label="错误信息" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error" style="color: #f56c6c">{{ row.error }}</span>
            <span v-else style="color: #909399">-</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="total > pageSize" style="margin-top: 12px; display: flex; justify-content: flex-end">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          small
          @current-change="loadData"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getTaskLogs } from '../api'

const items = ref<any[]>([])
const loading = ref(false)
const filterType = ref('')
const total = ref(0)
const page = ref(1)
const pageSize = 20

function taskTagType(type: string) {
  const map: Record<string, string> = {
    sync_full: 'primary',
    sync_fetch: 'success',
    sync_push: 'warning',
    sync_batch_push: 'info',
    dispatch: 'danger',
    email: 'success',
    closure: 'warning',
    email_pre_analysis: '',
    change_summary: 'info',
  }
  return map[type] || 'info'
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    success: '成功',
    failed: '失败',
    running: '进行中',
    fetched_only: '仅拉取',
    partial: '部分成功',
    pending: '进行中',
  }
  return map[status] || status
}

function statusTagType(status: string) {
  const map: Record<string, string> = {
    success: 'success',
    '成功': 'success',
    failed: 'danger',
    '失败': 'danger',
    running: 'warning',
    '进行中': 'warning',
    fetched_only: 'info',
    partial: 'warning',
    '部分成功': 'warning',
    pending: 'warning',
  }
  return map[status] || 'info'
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, any> = { limit: pageSize }
    if (filterType.value) params.task_type = filterType.value
    const res = await getTaskLogs(params)
    items.value = res.items || []
    total.value = res.total || 0
  } catch {
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleFilterChange() {
  page.value = 1
  loadData()
}

onMounted(loadData)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
  flex-wrap: wrap;
  gap: 8px;
}
.card-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 0;
  color: #333;
}
</style>
