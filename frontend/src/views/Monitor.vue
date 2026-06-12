<template>
  <div class="monitor-page">
    <div class="page-header">
      <h1>监控触发</h1>
    </div>

    <!-- Probe AITable data -->
    <div class="card">
      <div class="card-header">
        <h3>钉钉文档数据探测</h3>
        <el-button
          type="warning"
          :loading="probing"
          @click="handleProbe"
        >
          {{ probing ? '探测中...' : '探测钉钉文档数据' }}
        </el-button>
      </div>
      <div v-if="probeResult" class="probe-summary">
        <div class="probe-item">
          <span class="probe-label">待派单</span>
          <span class="probe-value">{{ probeResult.dispatch_count }}</span>
        </div>
        <div class="probe-item">
          <span class="probe-label">待发邮件</span>
          <span class="probe-value">{{ probeResult.email_count }}</span>
        </div>
        <span class="probe-time">探测时间: {{ probeResult.time }}</span>
      </div>
    </div>

    <!-- Dispatch pending from AITable -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <h3>待派单记录</h3>
        <div class="header-right">
          <span class="count-label">共 <strong>{{ dispatchPending.length }}</strong> 条待派单</span>
        </div>
      </div>
      <div v-if="dispatchLoading" v-loading="true" style="height: 80px"></div>
      <div v-else-if="dispatchPending.length === 0" class="empty-hint">暂无满足派单条件的记录</div>
      <el-table
        v-else
        :data="dispatchPending"
        stripe
        size="small"
        max-height="400"
      >
        <el-table-column prop="customer_name" label="客户名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="product" label="产品" width="80" show-overflow-tooltip />
        <el-table-column prop="supplier" label="伙伴供应商" width="120" show-overflow-tooltip />
        <el-table-column prop="partner_manager" label="伙伴负责人" width="100" show-overflow-tooltip />
        <el-table-column prop="engineer" label="工程师" width="100" show-overflow-tooltip />
        <el-table-column prop="dispatch_level" label="等级" width="60" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.dispatch_level" :type="row.dispatch_level === 'P1' ? 'danger' : row.dispatch_level === 'P2' ? 'warning' : 'info'" size="small">
              {{ row.dispatch_level }}
            </el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="PTS链接" width="70" align="center">
          <template #default="{ row }">
            <el-link v-if="row.pts_url" type="primary" :href="row.pts_url" target="_blank" :underline="false" size="small">查看</el-link>
            <span v-else class="text-muted">无</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              :loading="dispatchingMap[row.record_id]"
              :disabled="!!dispatchingMap[row.record_id] || !row.pts_url"
              @click="handleManualDispatch(row)"
            >
              {{ dispatchingMap[row.record_id] ? '派单中' : '派单' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- Email pending from AITable -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <h3>可发送邮件记录</h3>
        <div class="header-right">
          <span class="count-label">共 <strong>{{ emailPending.length }}</strong> 条可发送</span>
          <el-button
            v-if="emailPending.length > 0"
            size="small"
            type="success"
            :loading="runningPreAnalysis"
            @click="handleRunPreAnalysis"
          >
            {{ runningPreAnalysis ? '分析中...' : '运行预分析' }}
          </el-button>
        </div>
      </div>
      <div v-if="emailLoading" v-loading="true" style="height: 80px"></div>
      <div v-else-if="emailPending.length === 0" class="empty-hint">暂无满足邮件发送条件的记录（邮件未发送 + 有巡检报告）</div>
      <div v-else class="email-cards">
        <div v-for="item in emailPending" :key="item.record_id" class="email-card">
          <div class="email-card-header">
            <span class="email-card-customer">{{ item.customer_name }}</span>
            <el-tag size="small" type="info">{{ item.product_name }}</el-tag>
            <!-- Pre-analysis badge -->
            <el-tag
              v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success'"
              type="success"
              size="small"
              effect="dark"
              style="cursor: pointer"
              @click="showPreAnalysisDetail(item)"
            >
              已预分析 ▸
            </el-tag>
            <el-tag
              v-else-if="preAnalysisMap[item.record_id]?.analysis_status === 'pending'"
              type="warning"
              size="small"
            >
              分析中
            </el-tag>
            <el-tag
              v-else-if="preAnalysisMap[item.record_id]?.analysis_status === 'failed'"
              type="danger"
              size="small"
            >
              分析失败
            </el-tag>
            <el-tag v-else type="info" size="small">未预分析</el-tag>
          </div>
          <div class="email-card-body">
            <div class="email-card-row">
              <span class="row-label">收件邮箱</span>
              <span v-if="item.email_address_str" class="row-value email-text">{{ item.email_address_str }}</span>
              <span v-else class="row-value text-muted">无</span>
            </div>
            <!-- Show pre-analysis extracted emails if different -->
            <div v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success' && preAnalysisMap[item.record_id]?.emails" class="email-card-row">
              <span class="row-label">AI邮箱</span>
              <span class="row-value email-text">{{ preAnalysisMap[item.record_id].emails }}</span>
            </div>
            <div class="email-card-row">
              <span class="row-label">销售</span>
              <span class="row-value">{{ item.sales_name || '-' }}</span>
            </div>
            <div class="email-card-row">
              <span class="row-label">巡检报告</span>
              <span class="row-value">
                <el-tag type="success" size="small">PDF</el-tag>
                <span class="attachment-name" :title="(item.attachments || []).join('、')">
                  {{ (item.attachments || [])[0] || '-' }}
                </span>
                <span v-if="(item.attachments || []).length > 1" class="more-files">等{{ item.attachments.length }}个文件</span>
              </span>
            </div>
            <!-- Show pre-analysis summary preview -->
            <div v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success'" class="email-card-row">
              <span class="row-label">巡检日期</span>
              <span class="row-value">{{ preAnalysisMap[item.record_id]?.inspection_date || '-' }}</span>
            </div>
            <div v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success' && preAnalysisMap[item.record_id]?.summary" class="email-card-row">
              <span class="row-label">巡检总结</span>
              <span class="row-value summary-preview">{{ (preAnalysisMap[item.record_id]?.summary || '').slice(0, 100) }}{{ (preAnalysisMap[item.record_id]?.summary || '').length > 100 ? '...' : '' }}</span>
            </div>
          </div>
          <div class="email-card-footer">
            <el-button
              v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success'"
              type="warning"
              size="small"
              :loading="reAnalyzingMap[item.record_id]"
              @click="handleReAnalyze(item)"
            >
              {{ reAnalyzingMap[item.record_id] ? '重新分析中...' : '重新分析' }}
            </el-button>
            <el-button
              v-if="preAnalysisMap[item.record_id]?.analysis_status === 'success'"
              type="success"
              size="small"
              @click="handlePreviewSend(item)"
            >
              预览发送
            </el-button>
            <el-button
              type="primary"
              size="small"
              @click="handleSendEmail(item)"
            >
              {{ preAnalysisMap[item.record_id]?.analysis_status === 'success' ? '手动调整' : '发送邮件' }}
            </el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- Trigger logs -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <h3>触发日志</h3>
        <el-button size="small" @click="loadLogs">刷新</el-button>
      </div>
      <el-table :data="paginatedLogs" stripe size="small" v-loading="logsLoading">
        <el-table-column prop="trigger_type" label="类型" width="130">
          <template #default="{ row }">
            <el-tag :type="row.trigger_type === 'yunji_dispatch' ? 'primary' : row.trigger_type === 'closure_success' || row.trigger_type === 'closure_failed' ? 'warning' : 'success'" size="small">
              {{ row.trigger_type === 'yunji_dispatch' ? '云集派单' : row.trigger_type === 'closure_success' ? '工单闭环' : row.trigger_type === 'closure_failed' ? '闭环失败' : '巡检邮件' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="trigger_reason" label="原因" min-width="180" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="response_status" label="HTTP" width="70" />
        <el-table-column prop="created_at" label="时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
      </el-table>
      <div v-if="logs.length > logPageSize" style="margin-top: 12px; display: flex; justify-content: flex-end;">
        <el-pagination
          v-model:current-page="logPage"
          :page-size="logPageSize"
          :total="logs.length"
          layout="prev, pager, next"
          small
        />
      </div>
    </div>

    <!-- Pre-analysis detail dialog -->
    <el-dialog
      v-model="paDetailVisible"
      title="预分析详情"
      width="600px"
    >
      <div v-if="paDetailData" style="font-size: 14px; line-height: 1.8">
        <div style="margin-bottom: 12px">
          <strong style="color: #333">{{ paDetailData.customer_name }}</strong>
          <el-tag size="small" type="info" style="margin-left: 8px">{{ paDetailData.product_name }}</el-tag>
        </div>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="客户名称">{{ paDetailData.customer_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="产品名称">{{ paDetailData.product_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="巡检日期">{{ paDetailData.inspection_date || '-' }}</el-descriptions-item>
          <el-descriptions-item label="数量">{{ paDetailData.quantity || '-' }}</el-descriptions-item>
          <el-descriptions-item label="AI提取邮箱">{{ paDetailData.emails || '-' }}</el-descriptions-item>
          <el-descriptions-item label="巡检总结">
            <div style="white-space: pre-wrap; max-height: 200px; overflow-y: auto">{{ paDetailData.summary || '-' }}</div>
          </el-descriptions-item>
          <el-descriptions-item label="分析时间">{{ paDetailData.analyzed_at ? formatTime(paDetailData.analyzed_at) : '-' }}</el-descriptions-item>
          <el-descriptions-item label="分析状态">
            <el-tag :type="paDetailData.analysis_status === 'success' ? 'success' : paDetailData.analysis_status === 'failed' ? 'danger' : 'warning'" size="small">
              {{ paDetailData.analysis_status === 'success' ? '成功' : paDetailData.analysis_status === 'failed' ? '失败' : '进行中' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item v-if="paDetailData.error_message" label="错误信息">
            <span style="color: #f56c6c">{{ paDetailData.error_message }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </div>
      <template #footer>
        <el-button @click="paDetailVisible = false">关闭</el-button>
        <el-button
          type="warning"
          @click="handleReAnalyze(paDetailItem); paDetailVisible = false"
        >
          重新分析
        </el-button>
        <el-button type="primary" @click="handleSendEmail(paDetailItem); paDetailVisible = false">手动调整</el-button>
        <el-button
          v-if="paDetailData?.analysis_status === 'success'"
          type="success"
          @click="handlePreviewSend(paDetailItem); paDetailVisible = false"
        >
          预览发送
        </el-button>
      </template>
    </el-dialog>

    <!-- Email preview & confirm dialog -->
    <el-dialog
      v-model="emailPreviewVisible"
      title="邮件发送预览"
      width="680px"
      :close-on-click-modal="false"
    >
      <div v-if="emailPreviewLoading" v-loading="true" style="height: 100px"></div>
      <div v-else-if="emailPreviewData && emailPreviewData.status === 'error'" style="color: #f56c6c">
        {{ emailPreviewData.message }}
      </div>
      <div v-else-if="emailPreviewData" style="font-size: 14px; line-height: 1.8">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="客户名称">{{ emailPreviewData.customer_name }}</el-descriptions-item>
          <el-descriptions-item label="产品名称">{{ emailPreviewData.product_name }}</el-descriptions-item>
          <el-descriptions-item label="巡检日期">{{ emailPreviewData.inspection_date }}</el-descriptions-item>
          <el-descriptions-item label="数量">{{ emailPreviewData.quantity }}</el-descriptions-item>
          <el-descriptions-item label="销售">{{ emailPreviewData.sales_name || '-' }}</el-descriptions-item>
        </el-descriptions>
        <div style="margin-top: 16px">
          <div style="font-weight: bold; margin-bottom: 8px; color: #333">邮件主题</div>
          <div style="background: #f5f7fa; padding: 8px 12px; border-radius: 4px">{{ emailPreviewData.subject }}</div>
        </div>
        <div style="margin-top: 12px">
          <div style="font-weight: bold; margin-bottom: 8px; color: #333">收件人</div>
          <div style="background: #f5f7fa; padding: 8px 12px; border-radius: 4px">{{ (emailPreviewData.to_emails || []).join(', ') || '（无收件人）' }}</div>
        </div>
        <div style="margin-top: 12px">
          <div style="font-weight: bold; margin-bottom: 8px; color: #333">抄送人</div>
          <div style="background: #f5f7fa; padding: 8px 12px; border-radius: 4px">{{ (emailPreviewData.cc_emails || []).join(', ') }}</div>
        </div>
        <div style="margin-top: 12px">
          <div style="font-weight: bold; margin-bottom: 8px; color: #333">附件</div>
          <div style="background: #f5f7fa; padding: 8px 12px; border-radius: 4px">{{ (emailPreviewData.attachments || []).join('、') || '-' }}</div>
        </div>
        <div style="margin-top: 12px">
          <div style="font-weight: bold; margin-bottom: 8px; color: #333">邮件正文</div>
          <div style="background: #f5f7fa; padding: 12px; border-radius: 4px; white-space: pre-wrap; max-height: 300px; overflow-y: auto">{{ emailPreviewData.body }}</div>
        </div>
      </div>
      <template #footer>
        <el-button @click="emailPreviewVisible = false">取消</el-button>
        <el-button
          type="success"
          :loading="directSendingMap[emailPreviewItem?.record_id]"
          :disabled="!emailPreviewData || emailPreviewData.status === 'error' || (emailPreviewData.to_emails || []).length === 0"
          @click="handleConfirmSend"
        >
          确认发送
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getDispatchPending, manualDispatch, getEmailPending, getTriggerLogs, getPreAnalysisStatus, runPreAnalysis, reAnalyzeRecord, sendDirectEmail, previewEmailContent } from '../api'
import { useRouter } from 'vue-router'

const probing = ref(false)
const probeResult = ref<{ dispatch_count: number; email_count: number; time: string } | null>(null)

const dispatchPending = ref<any[]>([])
const dispatchLoading = ref(false)
const dispatchingMap = reactive<Record<string, boolean>>({})

const emailPending = ref<any[]>([])
const emailLoading = ref(false)
const preAnalysisMap = reactive<Record<string, any>>({})
const runningPreAnalysis = ref(false)
const directSendingMap = reactive<Record<string, boolean>>({})
const reAnalyzingMap = reactive<Record<string, boolean>>({})
const paDetailVisible = ref(false)
const paDetailData = ref<any>(null)
const paDetailItem = ref<any>(null)

const emailPreviewVisible = ref(false)
const emailPreviewLoading = ref(false)
const emailPreviewData = ref<any>(null)
const emailPreviewItem = ref<any>(null)

const router = useRouter()

const logs = ref<any[]>([])
const logsLoading = ref(false)
const logPage = ref(1)
const logPageSize = 5

const paginatedLogs = computed(() => {
  const start = (logPage.value - 1) * logPageSize
  return logs.value.slice(start, start + logPageSize)
})

async function handleProbe() {
  probing.value = true
  try {
    const [dispatchRes, emailRes] = await Promise.all([
      getDispatchPending(true),
      getEmailPending(true),
    ])
    const dCount = (dispatchRes.pending || []).length
    const eCount = (emailRes.pending || []).length

    dispatchPending.value = dispatchRes.pending || []
    emailPending.value = emailRes.pending || []

    probeResult.value = {
      dispatch_count: dCount,
      email_count: eCount,
      time: new Date().toLocaleString('zh-CN'),
    }
    // Also load pre-analysis data
    loadPreAnalysis()
    ElMessage.success(`探测完成: 待派单${dCount}条, 待发邮件${eCount}条`)
  } catch (e: any) {
    ElMessage.error('探测失败: ' + e.message)
  } finally {
    probing.value = false
  }
}

async function loadDispatchPending() {
  dispatchLoading.value = true
  try {
    const res = await getDispatchPending()
    dispatchPending.value = res.pending || []
  } catch {
    dispatchPending.value = []
  } finally {
    dispatchLoading.value = false
  }
}

async function handleManualDispatch(row: any) {
  if (dispatchingMap[row.record_id]) return
  dispatchingMap[row.record_id] = true
  try {
    const res = await manualDispatch(row.record_id)
    if (res.status === 'success') {
      ElMessage.success(res.message || '派单成功')
      loadDispatchPending()
      loadLogs()
    } else {
      ElMessage.warning(res.message || '派单未成功')
    }
  } catch (e: any) {
    ElMessage.error('派单失败: ' + e.message)
  } finally {
    dispatchingMap[row.record_id] = false
  }
}

async function loadEmailPending() {
  emailLoading.value = true
  try {
    const res = await getEmailPending()
    emailPending.value = res.pending || []
  } catch {
    emailPending.value = []
  } finally {
    emailLoading.value = false
  }
}

function handleSendEmail(row?: any) {
  if (row) {
    // Navigate to email tool with pre-filled data from this record
    const query: Record<string, string> = {
      record_id: row.record_id || '',
      customer_name: row.customer_name || '',
      product_name: row.product_name || '',
      emails: row.email_address_str || '',
      sales_name: row.sales_name || '',
    }
    // If pre-analysis exists, also pass pre_analysis_id for EmailTool to use cached data
    const pa = preAnalysisMap[row.record_id]
    if (pa) {
      query.pre_analysis = 'true'
    }
    router.push({ path: '/email-tool', query })
  } else {
    router.push('/email-tool')
  }
}

function showPreAnalysisDetail(item: any) {
  const pa = preAnalysisMap[item.record_id]
  if (!pa) return
  paDetailItem.value = item
  paDetailData.value = pa
  paDetailVisible.value = true
}

async function handleReAnalyze(row: any) {
  try {
    await ElMessageBox.confirm(
      '确认重新分析？将删除旧的预分析结果，重新下载PDF并AI提取。',
      '重新分析确认',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }

  reAnalyzingMap[row.record_id] = true
  try {
    const res = await reAnalyzeRecord(row.record_id)
    const result = res.result || {}
    if (result.success > 0) {
      ElMessage.success(`重新分析完成`)
    } else {
      ElMessage.warning(`重新分析未成功: 扫描${result.scanned}条，无新分析`)
    }
    await loadPreAnalysis()
  } catch (e: any) {
    ElMessage.error('重新分析失败: ' + e.message)
  } finally {
    reAnalyzingMap[row.record_id] = false
  }
}

async function loadPreAnalysis() {
  try {
    const res = await getPreAnalysisStatus()
    const pa = res.pre_analysis || {}
    // Clear and re-populate reactive map
    for (const key of Object.keys(preAnalysisMap)) {
      delete preAnalysisMap[key]
    }
    for (const [recordId, data] of Object.entries(pa)) {
      preAnalysisMap[recordId] = data
    }
  } catch {
    // Ignore pre-analysis loading errors
  }
}

async function handleRunPreAnalysis() {
  runningPreAnalysis.value = true
  try {
    const res = await runPreAnalysis()
    const result = res.result || {}
    const msg = result.skipped === result.scanned
      ? `预分析完成: ${result.scanned}条已分析，无需重复处理`
      : `预分析完成: 新分析${result.success || 0}条, 跳过${result.skipped || 0}条已分析记录`
    ElMessage.success(msg)
    await loadPreAnalysis()
  } catch (e: any) {
    ElMessage.error('预分析失败: ' + e.message)
  } finally {
    runningPreAnalysis.value = false
  }
}

async function handlePreviewSend(row: any) {
  const pa = preAnalysisMap[row.record_id]
  if (!pa || pa.analysis_status !== 'success') {
    ElMessage.warning('该记录未预分析，无法发送邮件')
    return
  }

  emailPreviewItem.value = row
  emailPreviewLoading.value = true
  emailPreviewVisible.value = true
  emailPreviewData.value = null

  try {
    const res = await previewEmailContent(row.record_id)
    emailPreviewData.value = res
    if (res.status === 'error') {
      ElMessage.warning(res.message)
    }
  } catch (e: any) {
    emailPreviewData.value = { status: 'error', message: '预览加载失败: ' + e.message }
  } finally {
    emailPreviewLoading.value = false
  }
}

async function handleConfirmSend() {
  const row = emailPreviewItem.value
  if (!row) return

  directSendingMap[row.record_id] = true
  try {
    const res = await sendDirectEmail({
      record_id: row.record_id,
      extra_emails: row.email_address_str || '',
    })
    if (res.status === 'success') {
      ElMessage.success('邮件发送成功')
      if (res.closure) {
        if (res.closure.success) {
          ElMessage.success('工单已自动闭环: ' + res.closure.message)
        } else {
          ElMessage.warning('工单闭环未成功: ' + res.closure.message)
        }
      }
      emailPreviewVisible.value = false
      // Refresh data
      loadEmailPending()
      loadPreAnalysis()
      loadLogs()
    } else {
      ElMessage.error(res.message || '发送失败')
    }
  } catch (e: any) {
    ElMessage.error('发送失败: ' + e.message)
  } finally {
    directSendingMap[row.record_id] = false
  }
}

async function loadLogs() {
  logsLoading.value = true
  try {
    const res = await getTriggerLogs(100)
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

onMounted(() => {
  loadDispatchPending()
  loadEmailPending()
  loadPreAnalysis()
  loadLogs()
})
</script>

<style scoped>
.card h3 {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
  color: #333;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.card-header h3 {
  margin-bottom: 0;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.count-label {
  font-size: 13px;
  color: #666;
}

.count-label strong {
  color: #e6a23c;
  font-size: 16px;
}

.probe-summary {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 12px 0;
}

.probe-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.probe-label {
  font-size: 13px;
  color: #666;
}

.probe-value {
  font-size: 20px;
  font-weight: 700;
  color: #e6a23c;
}

.probe-time {
  font-size: 12px;
  color: #999;
  margin-left: auto;
}

.empty-hint {
  text-align: center;
  color: #999;
  padding: 24px 0;
  font-size: 13px;
}

.text-muted {
  color: #ccc;
  font-size: 12px;
}

.attachment-info {
  display: flex;
  align-items: center;
  gap: 6px;
}

.attachment-names {
  font-size: 12px;
  color: #666;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}

.email-text {
  font-size: 12px;
  color: #409eff;
  word-break: break-all;
}

.email-cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.email-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 14px 16px;
  transition: box-shadow 0.2s;
}

.email-card:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.email-card-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.email-card-customer {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

.email-card-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
}

.email-card-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.email-card-row .row-label {
  color: #909399;
  min-width: 60px;
  flex-shrink: 0;
}

.email-card-row .row-value {
  color: #606266;
  display: flex;
  align-items: center;
  gap: 6px;
}

.attachment-name {
  font-size: 12px;
  color: #606266;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 300px;
}

.more-files {
  font-size: 12px;
  color: #909399;
}

.email-card-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.summary-preview {
  font-size: 12px;
  color: #909399;
  max-width: 400px;
  line-height: 1.4;
}
</style>
