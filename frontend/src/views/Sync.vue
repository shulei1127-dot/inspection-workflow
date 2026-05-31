<template>
  <div class="sync-page">
    <div class="page-header">
      <h1>数据同步</h1>
    </div>

    <!-- Sync logs -->
    <div class="card">
      <div class="card-header">
        <h3>同步日志</h3>
        <el-button size="small" @click="loadLogs">刷新</el-button>
      </div>
      <el-table :data="logs" stripe size="small" v-loading="logsLoading">
        <el-table-column prop="trigger_source" label="触发源" width="100">
          <template #default="{ row }">
            <el-tag :type="row.trigger_source === 'scheduler' ? 'info' : 'primary'" size="small">
              {{ row.trigger_source === 'scheduler' ? '定时' : '手动' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sync_month" label="月份" width="90" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' || row.status === 'fetched_only' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="fetched_count" label="拉取" width="60" />
        <el-table-column prop="created_count" label="新建" width="60" />
        <el-table-column prop="updated_count" label="更新" width="60" />
        <el-table-column prop="skipped_count" label="跳过" width="60" />
        <el-table-column prop="error_message" label="错误" min-width="150" show-overflow-tooltip />
        <el-table-column prop="started_at" label="开始时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.started_at) }}
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getSyncLogs } from '../api'

const logs = ref<any[]>([])
const logsLoading = ref(false)

async function loadLogs() {
  logsLoading.value = true
  try {
    const res = await getSyncLogs(100)
    logs.value = Array.isArray(res) ? res : []
  } catch {
    logs.value = []
  } finally {
    logsLoading.value = false
  }
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 19)
}

onMounted(loadLogs)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.card-header h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 0;
  color: #333;
}
</style>
