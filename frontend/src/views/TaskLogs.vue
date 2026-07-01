<template>
  <div class="task-logs-page">
    <div class="page-header">
      <h1>📋 任务日志</h1>
      <div class="header-actions">
        <el-select v-model="filterType" placeholder="任务类型" clearable style="width: 160px; margin-right: 8px">
          <el-option label="PTS完整同步" value="sync_full" />
          <el-option label="PTS数据拉取" value="sync_fetch" />
          <el-option label="AITable推送" value="sync_push" />
          <el-option label="云集派单" value="dispatch" />
          <el-option label="巡检邮件" value="email" />
          <el-option label="工单闭环" value="closure" />
          <el-option label="邮件预分析" value="email_pre_analysis" />
          <el-option label="变更日报推送" value="change_summary" />
        </el-select>
        <el-button type="primary" @click="fetchLogs">刷新</el-button>
      </div>
    </div>

    <div class="card">
      <el-table :data="logs" stripe style="width: 100%">
        <el-table-column prop="task_label" label="任务类型" width="130" />
        <el-table-column prop="trigger_source" label="触发" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.trigger_source === '定时' ? 'info' : 'warning'" size="small">{{ row.trigger_source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="time" label="时间" width="170" />
        <el-table-column prop="summary" label="摘要" min-width="300" />
        <el-table-column prop="error" label="错误" width="200">
          <template #default="{ row }">
            <span v-if="row.error" style="color: #ff4d4f; font-size: 12px">{{ row.error.slice(0, 60) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getTaskLogs } from '../api'

const logs = ref<any[]>([])
const filterType = ref('')

const statusTagType = (s: string) => {
  if (s === 'success' || s === '成功' || s === 'completed') return 'success'
  if (s === 'failed' || s === '失败') return 'danger'
  if (s === 'running' || s === '进行中') return 'warning'
  return 'info'
}

const statusLabel = (s: string) => {
  const map: Record<string, string> = { success: '成功', failed: '失败', running: '运行中', completed: '完成' }
  return map[s] || s
}

async function fetchLogs() {
  try {
    const res = await getTaskLogs(filterType.value || undefined, 100)
    logs.value = res.items || []
  } catch (e: any) {
    ElMessage.error('加载失败: ' + e.message)
  }
}

onMounted(fetchLogs)
</script>
