<template>
  <div class="visit-page">
    <div class="page-header">
      <h1>售后回访闭环</h1>
      <span class="page-desc">人工电话回访后，系统自动创建并闭环 PTS 回访工单</span>
    </div>

    <!-- 统计卡片 -->
    <div class="stat-row">
      <div class="stat-card">
        <div class="label">待自动闭环</div>
        <div class="value info">{{ pendingTotal }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已完成</div>
        <div class="value success">{{ logsStats.completed }}</div>
      </div>
      <div class="stat-card">
        <div class="label">执行中</div>
        <div class="value warning">{{ logsStats.running }}</div>
      </div>
      <div class="stat-card">
        <div class="label">失败</div>
        <div class="value danger">{{ logsStats.failed }}</div>
      </div>
    </div>

    <!-- 待闭环项目（来自 AITable） -->
    <div class="card">
      <div class="section-header">
        <h3>满足闭环条件的记录 <el-tag size="small" type="info">{{ pendingTotal }}</el-tag></h3>
        <div class="section-actions">
          <el-button @click="loadPending(true)" :loading="pendingLoading" size="small">刷新</el-button>
          <el-button type="primary" :loading="batchRunning" @click="handleBatchTrigger" size="small">
            {{ batchRunning ? '批量执行中...' : '批量触发' }}
          </el-button>
        </div>
      </div>
      <p class="section-hint">条件：回访状态=已回访 且 回访类型/满意度/备注不为空 且 回访链接为空</p>

      <el-table :data="pendingItems" stripe v-loading="pendingLoading" size="small" empty-text="暂无满足条件的记录">
        <el-table-column prop="customer_name" label="客户名称" min-width="140" show-overflow-tooltip />
        <el-table-column label="PTS交付" width="80" align="center">
          <template #default="{ row }">
            <a v-if="row.pts_link" :href="row.pts_link" target="_blank" rel="noopener" class="pts-link">查看</a>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="visit_type" label="回访类型" width="130" show-overflow-tooltip />
        <el-table-column prop="satisfaction" label="满意度" width="100" />
        <el-table-column prop="visit_owner" label="回访人" width="80" />
        <el-table-column prop="region" label="区域" width="100" show-overflow-tooltip />
        <el-table-column prop="feedback_note" label="备注" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              :loading="triggeringMap[row.record_id]"
              @click="handleTriggerSingle(row)"
            >
              执行闭环
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 执行日志 -->
    <div class="card" style="margin-top: 16px;">
      <div class="section-header">
        <h3>执行日志</h3>
        <el-button @click="loadLogs" size="small">刷新</el-button>
      </div>

      <el-table :data="logs" stripe v-loading="logsLoading" size="small" empty-text="暂无执行记录">
        <el-table-column prop="customer_name" label="客户" min-width="130" show-overflow-tooltip />
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="步骤进度" min-width="180">
          <template #default="{ row }">
            <div class="step-progress">
              <span
                v-for="step in stepList"
                :key="step.key"
                :class="['step-dot', row[step.key]]"
                :title="step.label + ': ' + row[step.key]"
              >●</span>
              <span class="step-labels">
                <span v-for="step in stepList" :key="step.key" class="step-label">{{ step.short }}</span>
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="回访链接" width="80" align="center">
          <template #default="{ row }">
            <a v-if="row.visit_url" :href="row.visit_url" target="_blank" rel="noopener" class="pts-link">查看</a>
            <span v-else style="color: #999">-</span>
          </template>
        </el-table-column>
        <el-table-column label="满意度" width="70" align="center">
          <template #default="{ row }">
            <span v-if="row.satisfaction_score">{{ row.satisfaction_score }}/5</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error" style="color: #f56c6c; font-size: 12px">{{ row.error }}</span>
            <span v-else style="color: #999">-</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="165">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'failed' || row.status === 'partial'"
              type="primary"
              size="small"
              :loading="retryingMap[row.id]"
              @click="handleRetry(row)"
            >重试</el-button>
            <el-button size="small" link @click="handleDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination
          v-model:current-page="logsPage"
          :page-size="logsPageSize"
          :total="logsTotal"
          layout="total, prev, pager, next"
          @current-change="loadLogs"
        />
      </div>
    </div>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="回访详情" width="700px" destroy-on-close>
      <div v-if="detailData" class="detail-content">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="客户">{{ detailData.customer_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="区域">{{ detailData.region || '-' }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(detailData.status)" size="small">{{ statusLabel(detailData.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="PTS 回访 ID">{{ detailData.pts_visit_id || '-' }}</el-descriptions-item>
          <el-descriptions-item label="回访链接">
            <a v-if="detailData.visit_url" :href="detailData.visit_url" target="_blank" rel="noopener" class="pts-link">{{ detailData.visit_url }}</a>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="满意度">{{ detailData.satisfaction_score ? detailData.satisfaction_score + '/5' : '-' }}</el-descriptions-item>
          <el-descriptions-item label="重试次数">{{ detailData.retry_count || 0 }}</el-descriptions-item>
          <el-descriptions-item label="触发来源">{{ detailData.trigger_source || '-' }}</el-descriptions-item>
        </el-descriptions>

        <h4 style="margin: 16px 0 8px">步骤执行状态</h4>
        <el-table :data="stepDetailList" size="small" border>
          <el-table-column prop="label" label="步骤" width="140" />
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="stepStatusType(row.status)" size="small">{{ row.status || 'pending' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="detailData.error" style="margin-top: 12px">
          <h4 style="margin: 0 0 4px; color: #f56c6c">错误信息</h4>
          <pre style="background: #fef0f0; padding: 8px; border-radius: 4px; font-size: 12px; white-space: pre-wrap; color: #f56c6c">{{ detailData.error }}</pre>
        </div>

        <div v-if="detailData.step_log && detailData.step_log.length" style="margin-top: 12px">
          <h4 style="margin: 0 0 8px">执行日志</h4>
          <el-timeline>
            <el-timeline-item
              v-for="(entry, i) in detailData.step_log"
              :key="i"
              :type="entry.status === 'success' ? 'success' : entry.status === 'failed' ? 'danger' : 'primary'"
              :timestamp="formatTime(entry.timestamp)"
            >
              {{ entry.step }} — {{ entry.status }}
              <span v-if="entry.error" style="color: #f56c6c">: {{ entry.error }}</span>
            </el-timeline-item>
          </el-timeline>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getVisitLogs, getVisitDetail, triggerVisit, retryVisit, batchTriggerVisits } from '../api'

// ── AITable 待闭环记录 ──
const pendingItems = ref<any[]>([])
const pendingTotal = computed(() => pendingItems.value.length)
const pendingLoading = ref(false)
const batchRunning = ref(false)
const triggeringMap = reactive<Record<string, boolean>>({})

// ── 执行日志 ──
const logs = ref<any[]>([])
const logsTotal = ref(0)
const logsPage = ref(1)
const logsPageSize = 50
const logsLoading = ref(false)
const retryingMap = reactive<Record<string, boolean>>({})

// ── 详情弹窗 ──
const detailVisible = ref(false)
const detailData = ref<any>(null)

// 步骤定义
const stepList = [
  { key: 'step_fetch_delivery', label: '查询交付', short: '查' },
  { key: 'step_create_visit', label: '创建回访', short: '建' },
  { key: 'step_find_visit', label: '定位回访', short: '找' },
  { key: 'step_fill_feedback', label: '填写反馈', short: '填' },
  { key: 'step_finish_visit', label: '完成回访', short: '完' },
  { key: 'step_post_check', label: '后验证', short: '验' },
  { key: 'step_dingtalk_writeback', label: '钉钉写入', short: '写' },
]

const stepDetailList = computed(() => {
  if (!detailData.value) return []
  return stepList.map(s => ({
    label: s.label,
    status: (detailData.value as any)[s.key] || 'pending',
  }))
})

const logsStats = computed(() => {
  const items = logs.value
  return {
    completed: items.filter(r => r.status === 'completed').length,
    running: items.filter(r => r.status === 'running' || r.status === 'partial').length,
    failed: items.filter(r => r.status === 'failed').length,
  }
})

function statusType(s: string) {
  if (s === 'completed') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'running' || s === 'partial') return 'warning'
  return 'info'
}

function statusLabel(s: string) {
  const map: Record<string, string> = {
    pending: '待处理', running: '执行中', completed: '已完成',
    failed: '失败', partial: '部分完成',
  }
  return map[s] || s
}

function stepStatusType(s: string) {
  if (s === 'success') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'running') return 'warning'
  if (s === 'skipped') return 'info'
  return ''
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 19)
}

// ── 加载 AITable 待闭环数据 ──
async function loadPending(forceRefresh = false) {
  pendingLoading.value = true
  try {
    const url = forceRefresh ? '/api/visit/pending?force=true' : '/api/visit/pending'
    const res: any = await fetch(url).then(r => r.json())
    pendingItems.value = res.items || []
  } catch {
    // silent
  } finally {
    pendingLoading.value = false
  }
}

// ── 加载执行日志 ──
async function loadLogs() {
  logsLoading.value = true
  try {
    const res: any = await getVisitLogs({
      limit: logsPageSize,
      offset: (logsPage.value - 1) * logsPageSize,
    })
    logs.value = res.items || []
    logsTotal.value = res.total || 0
  } catch {
    // silent
  } finally {
    logsLoading.value = false
  }
}

// ── 单条执行闭环 ──
async function handleTriggerSingle(row: any) {
  if (triggeringMap[row.record_id]) return
  triggeringMap[row.record_id] = true
  try {
    const res: any = await triggerVisit(row.record_id)
    if (res.status === 'completed') {
      ElMessage.success(`${row.customer_name}: 回访闭环完成`)
    } else if (res.status === 'skipped') {
      ElMessage.warning(res.reason || '回访未启用')
    } else {
      ElMessage.warning(`状态: ${res.status} ${res.error || ''}`)
    }
    loadPending()
    loadLogs()
  } catch (e: any) {
    ElMessage.error('触发闭环失败: ' + e.message)
  } finally {
    triggeringMap[row.record_id] = false
  }
}

// ── 批量触发 ──
async function handleBatchTrigger() {
  batchRunning.value = true
  try {
    const res: any = await batchTriggerVisits()
    if (res.total === 0) {
      ElMessage.info('当前无满足条件的记录')
    } else {
      ElMessage.success(`批量闭环: 完成${res.completed} 失败${res.failed} 跳过${res.skipped}`)
    }
    loadPending()
    loadLogs()
  } catch (e: any) {
    ElMessage.error('批量闭环失败: ' + e.message)
  } finally {
    batchRunning.value = false
  }
}

// ── 重试 ──
async function handleRetry(row: any) {
  if (retryingMap[row.id]) return
  retryingMap[row.id] = true
  try {
    const res: any = await retryVisit(row.id)
    if (res.status === 'completed') {
      ElMessage.success('重试成功，回访已完成')
    } else {
      ElMessage.warning(`重试状态: ${res.status} ${res.error || ''}`)
    }
    loadLogs()
    loadPending()
  } catch (e: any) {
    ElMessage.error('重试失败: ' + e.message)
  } finally {
    retryingMap[row.id] = false
  }
}

// ── 详情 ──
async function handleDetail(row: any) {
  try {
    const res: any = await getVisitDetail(row.id)
    detailData.value = res
    detailVisible.value = true
  } catch (e: any) {
    ElMessage.error('获取详情失败: ' + e.message)
  }
}

onMounted(() => {
  loadPending()
  loadLogs()
})
</script>

<style scoped>
.visit-page {
  max-width: 1400px;
}
.page-desc {
  font-size: 13px;
  color: #8c8c8c;
  margin-left: 12px;
}
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.section-header h3 {
  margin: 0;
  font-size: 16px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-actions {
  display: flex;
  gap: 8px;
}
.section-hint {
  font-size: 12px;
  color: #8c8c8c;
  margin: 0 0 12px 0;
}
.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
.pts-link {
  color: #409eff;
  text-decoration: none;
  font-size: 13px;
}
.pts-link:hover {
  text-decoration: underline;
}
.step-progress {
  display: flex;
  align-items: center;
  gap: 2px;
}
.step-dot {
  font-size: 14px;
  cursor: default;
}
.step-dot.pending { color: #d9d9d9; }
.step-dot.running { color: #e6a23c; }
.step-dot.success { color: #67c23a; }
.step-dot.failed { color: #f56c6c; }
.step-dot.skipped { color: #909399; }
.step-labels {
  display: flex;
  gap: 0;
  margin-left: 6px;
}
.step-label {
  font-size: 10px;
  color: #999;
  width: 16px;
  text-align: center;
}
.detail-content h4 {
  color: #333;
  font-size: 14px;
}
</style>
