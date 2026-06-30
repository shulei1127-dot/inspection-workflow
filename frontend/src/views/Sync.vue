<template>
  <div class="sync-page">
    <div class="page-header">
      <h1>数据同步</h1>
    </div>

    <!-- Sync logs -->
    <div class="card">
      <div class="card-header">
        <h3>同步日志</h3>
        <div style="display: flex; gap: 8px; align-items: center">
          <el-radio-group v-model="filterType" size="small" @change="handleFilterChange">
            <el-radio-button label="">全部</el-radio-button>
            <el-radio-button label="full_sync">完整同步</el-radio-button>
            <el-radio-button label="fetch_only">仅拉取</el-radio-button>
            <el-radio-button label="push_only">仅推送</el-radio-button>
            <el-radio-button label="batch_push">批量推送</el-radio-button>
          </el-radio-group>
          <el-button size="small" @click="loadLogs">刷新</el-button>
        </div>
      </div>
      <el-table :data="logs" stripe size="small" v-loading="logsLoading">
        <el-table-column prop="started_at" label="时间" width="170" />
        <el-table-column prop="sync_type" label="操作类型" width="110">
          <template #default="{ row }">
            <el-tag :type="syncTypeTagType(row.sync_type)" size="small">{{ syncTypeLabel(row.sync_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="trigger_source" label="触发源" width="80">
          <template #default="{ row }">
            <el-tag :type="row.trigger_source === 'scheduler' ? 'info' : 'primary'" size="small">
              {{ row.trigger_source === 'scheduler' ? '定时' : '手动' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sync_month" label="月份" width="90" />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="fetched_count" label="拉取" width="60" />
        <el-table-column prop="created_count" label="新建" width="60" />
        <el-table-column prop="updated_count" label="更新" width="60" />
        <el-table-column prop="skipped_count" label="跳过" width="60" />
        <el-table-column prop="error_message" label="错误" min-width="150" show-overflow-tooltip />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getSyncLogs } from '../api'

const logs = ref<any[]>([])
const logsLoading = ref(false)
const filterType = ref('')

function syncTypeLabel(type: string) {
  const map: Record<string, string> = {
    full_sync: '完整同步',
    fetch_only: '仅拉取',
    push_only: '仅推送',
    batch_push: '批量推送',
    adjust_deadline: '月末调整',
  }
  return map[type] || type
}

function syncTypeTagType(type: string) {
  const map: Record<string, string> = {
    full_sync: 'primary',
    fetch_only: 'success',
    push_only: 'warning',
    batch_push: 'info',
    adjust_deadline: '',
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
  }
  return map[status] || status
}

function statusTagType(status: string) {
  const map: Record<string, string> = {
    success: 'success',
    failed: 'danger',
    running: 'warning',
    fetched_only: 'info',
    partial: 'warning',
  }
  return map[status] || 'info'
}

async function loadLogs() {
  logsLoading.value = true
  try {
    const res = await getSyncLogs(100, filterType.value || undefined)
    logs.value = Array.isArray(res) ? res : []
  } catch {
    logs.value = []
  } finally {
    logsLoading.value = false
  }
}

function handleFilterChange() {
  loadLogs()
}

onMounted(loadLogs)
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
