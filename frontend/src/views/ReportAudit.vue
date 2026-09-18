<template>
  <div class="report-audit-page">
    <div class="page-header">
      <div>
        <h1>🧾 巡检报告审核</h1>
        <p class="subtitle">独立只读模块：不会修改钉钉、发送邮件或推进 PTS 工单</p>
      </div>
      <el-button type="primary" :loading="scanning" @click="handleScan">
        {{ scanning ? '审核中...' : '审核全部待发报告' }}
      </el-button>
    </div>

    <el-alert
      title="当前为准确性观察阶段"
      description="周期扫描默认关闭；点击按钮将审核“邮件是否发送=否”且已上传巡检报告的全部记录。审核结果不参与现有业务判断。"
      type="info"
      :closable="false"
      show-icon
      class="phase-alert"
    />

    <div class="stat-row">
      <div class="stat-card"><div class="label">已审核</div><div class="value info">{{ stats.total || 0 }}</div></div>
      <div class="stat-card"><div class="label">通过</div><div class="value success">{{ stats.passed || 0 }}</div></div>
      <div class="stat-card"><div class="label">有警告</div><div class="value warning">{{ stats.warning || 0 }}</div></div>
      <div class="stat-card"><div class="label">不通过</div><div class="value danger">{{ stats.rejected || 0 }}</div></div>
      <div class="stat-card"><div class="label">审核异常</div><div class="value">{{ stats.failed || 0 }}</div></div>
    </div>

    <div class="card filter-bar">
      <el-form :inline="true" @submit.prevent="applyFilters">
        <el-form-item label="客户名称">
          <el-input v-model="filters.customer_name" placeholder="模糊搜索" clearable style="width: 240px" />
        </el-form-item>
        <el-form-item label="审核状态">
          <el-select v-model="filters.status" clearable placeholder="全部" style="width: 140px">
            <el-option label="通过" value="passed" />
            <el-option label="有警告" value="warning" />
            <el-option label="不通过" value="rejected" />
            <el-option label="审核异常" value="failed" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="applyFilters">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="card">
      <div class="table-note">规则版本：{{ stats.rule_version || '-' }}。AI 结论必须结合证据人工抽查。</div>
      <el-table :data="items" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="customer_name" label="客户名称" min-width="220" show-overflow-tooltip />
        <el-table-column prop="product_name" label="产品" min-width="180" show-overflow-tooltip />
        <el-table-column prop="filename" label="审核文件" min-width="260" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="score" label="得分" width="80" />
        <el-table-column label="问题" width="190">
          <template #default="{ row }">
            <el-tag v-if="row.blocker_count" type="danger" size="small">阻断 {{ row.blocker_count }}</el-tag>
            <el-tag v-if="row.error_count" type="danger" effect="plain" size="small">错误 {{ row.error_count }}</el-tag>
            <el-tag v-if="row.warning_count" type="warning" size="small">警告 {{ row.warning_count }}</el-tag>
            <span v-if="!row.blocker_count && !row.error_count && !row.warning_count">—</span>
          </template>
        </el-table-column>
        <el-table-column label="审核方式" width="100">
          <template #default="{ row }">{{ row.ai_used ? '规则 + AI' : '仅规则' }}</template>
        </el-table-column>
        <el-table-column prop="reviewed_at" label="审核时间" width="170" />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="showDetail(row)">详情</el-button>
            <el-button link type="warning" :loading="recheckingId === row.id" @click="handleRecheck(row)">重审</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination">
        <el-pagination
          background
          layout="total, prev, pager, next"
          :total="total"
          :page-size="filters.limit"
          :current-page="page"
          @current-change="onPageChange"
        />
      </div>
    </div>

    <el-dialog v-model="detailVisible" title="巡检报告审核详情" width="82%" destroy-on-close>
      <template v-if="selected">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="客户">{{ selected.customer_name }}</el-descriptions-item>
          <el-descriptions-item label="产品">{{ selected.product_name }}</el-descriptions-item>
          <el-descriptions-item label="文件">{{ selected.filename }}</el-descriptions-item>
          <el-descriptions-item label="得分">{{ selected.score ?? '-' }}</el-descriptions-item>
          <el-descriptions-item label="规则版本">{{ selected.rule_version }}</el-descriptions-item>
          <el-descriptions-item label="文档信息">{{ documentInfo(selected) }}</el-descriptions-item>
        </el-descriptions>
        <el-alert v-if="selected.llm_summary" :title="selected.llm_summary" type="info" :closable="false" class="detail-alert" />
        <el-alert v-if="selected.error_message" :title="selected.error_message" type="error" :closable="false" class="detail-alert" />

        <el-empty v-if="!(selected.findings || []).length" description="未发现问题" />
        <div v-else class="finding-list">
          <div v-for="(finding, index) in selected.findings" :key="`${finding.rule_id}-${index}`" class="finding-card">
            <div class="finding-title">
              <div>
                <el-tag :type="severityType(finding.severity)" size="small">{{ severityText(finding.severity) }}</el-tag>
                <el-tag effect="plain" size="small">{{ finding.source === 'ai' ? 'AI' : '规则' }}</el-tag>
                <span class="rule-id">{{ finding.rule_id }}</span>
                <strong>{{ finding.title }}</strong>
              </div>
              <span v-if="finding.page" class="page-number">第 {{ finding.page }} 页</span>
            </div>
            <div class="finding-row"><span class="finding-label">证据</span><span>{{ finding.evidence }}</span></div>
            <div class="finding-row"><span class="finding-label">建议</span><span>{{ finding.suggestion }}</span></div>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getReportAudits, getReportAuditStats, recheckReportAudit, scanReportAudits } from '../api'

const items = ref<any[]>([])
const total = ref(0)
const stats = ref<any>({})
const loading = ref(false)
const scanning = ref(false)
const recheckingId = ref('')
const page = ref(1)
const detailVisible = ref(false)
const selected = ref<any>(null)
const filters = reactive({ customer_name: '', status: '', limit: 50 })

const statusText = (value: string) => ({ passed: '通过', warning: '有警告', rejected: '不通过', failed: '异常', running: '审核中', pending: '待审核' } as any)[value] || value
const statusType = (value: string) => ({ passed: 'success', warning: 'warning', rejected: 'danger', failed: 'danger', running: 'info', pending: 'info' } as any)[value] || 'info'
const severityText = (value: string) => ({ blocker: '阻断', error: '错误', warning: '警告', info: '提示' } as any)[value] || value
const severityType = (value: string) => ({ blocker: 'danger', error: 'danger', warning: 'warning', info: 'info' } as any)[value] || 'info'

function documentInfo(row: any) {
  const meta = row.document_meta || {}
  const parts = [String(meta.file_type || '').toUpperCase()]
  if (meta.page_count) parts.push(`${meta.page_count} 页`)
  if (meta.text_length) parts.push(`${meta.text_length} 字符`)
  return parts.filter(Boolean).join(' / ') || '-'
}

async function loadData() {
  loading.value = true
  try {
    const params: any = { ...filters, offset: (page.value - 1) * filters.limit }
    const [list, summary] = await Promise.all([getReportAudits(params), getReportAuditStats()])
    items.value = list.items || []
    total.value = list.total || 0
    stats.value = summary
  } catch (error: any) {
    ElMessage.error('加载失败：' + error.message)
  } finally {
    loading.value = false
  }
}

async function handleScan() {
  scanning.value = true
  try {
    const result = await scanReportAudits(100)
    ElMessage.success(`扫描完成：符合条件 ${result.eligible} 条，审核 ${result.reviewed} 条，跳过 ${result.skipped} 条`)
    await loadData()
  } catch (error: any) {
    ElMessage.error('扫描失败：' + error.message)
  } finally {
    scanning.value = false
  }
}

async function handleRecheck(row: any) {
  recheckingId.value = row.id
  try {
    await recheckReportAudit(row.id)
    ElMessage.success('重新审核完成')
    await loadData()
  } catch (error: any) {
    ElMessage.error('重新审核失败：' + error.message)
  } finally {
    recheckingId.value = ''
  }
}

function showDetail(row: any) {
  selected.value = row
  detailVisible.value = true
}

function applyFilters() {
  page.value = 1
  loadData()
}

function resetFilters() {
  filters.customer_name = ''
  filters.status = ''
  page.value = 1
  loadData()
}

function onPageChange(value: number) {
  page.value = value
  loadData()
}

onMounted(loadData)
</script>

<style scoped>
.subtitle { margin-top: 6px; color: #909399; font-size: 13px; }
.phase-alert { margin-bottom: 18px; }
.filter-bar { margin-bottom: 16px; padding-bottom: 2px; }
.table-note { padding: 0 0 14px; color: #909399; font-size: 13px; }
.pagination { display: flex; justify-content: flex-end; padding-top: 16px; }
.el-tag + .el-tag { margin-left: 5px; }
.detail-alert { margin: 16px 0; }
.finding-list { display: grid; gap: 12px; margin-top: 16px; }
.finding-card { border: 1px solid #ebeef5; border-radius: 8px; padding: 14px 16px; background: #fafafa; }
.finding-title { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.finding-title strong { margin-left: 8px; }
.rule-id { margin-left: 10px; color: #909399; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.page-number { color: #606266; white-space: nowrap; }
.finding-row { display: grid; grid-template-columns: 48px 1fr; gap: 8px; margin-top: 8px; line-height: 1.65; white-space: pre-wrap; }
.finding-label { color: #909399; }
@media (max-width: 900px) {
  .finding-title { align-items: flex-start; flex-direction: column; }
}
</style>
