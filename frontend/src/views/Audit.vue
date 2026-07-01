<template>
  <div class="audit-page">
    <div class="page-header">
      <h1>交付转售后审核</h1>
    </div>

    <!-- 操作栏 -->
    <div class="card action-bar">
      <el-button type="primary" :loading="running" @click="handleRun">
        {{ running ? '审核中...' : '执行审核' }}
      </el-button>
      <el-button @click="loadPending" :loading="pendingLoading">查看待审核项目</el-button>
    </div>

    <!-- 待审核项目（可折叠） -->
    <div v-if="pendingVisible" class="card">
      <h3 style="margin: 0 0 12px 0">待审核项目 ({{ pendingProjects.length }})</h3>
      <el-table :data="pendingProjects" stripe v-loading="pendingLoading" size="small">
        <el-table-column prop="project_name" label="项目名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="customer_name" label="客户" min-width="140" show-overflow-tooltip />
        <el-table-column prop="delivery_stage" label="交付阶段" width="120" />
        <el-table-column prop="assigner_name" label="交付分配人" width="110" />
        <el-table-column prop="person_in_charge_name" label="交付负责人" width="110" />
        <el-table-column prop="after_sales_leader" label="售后负责人" width="100" />
        <el-table-column label="PTS链接" width="90" align="center">
          <template #default="{ row }">
            <a :href="'https://pts.chaitin.net/project/' + row.project_id + '#base'" target="_blank" rel="noopener" class="pts-link">查看</a>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button
              type="primary"
              size="small"
              :loading="auditingMap[row.project_id]"
              @click="handleAuditSingle(row)"
            >
              {{ auditingMap[row.project_id] ? '审核中' : '审核' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 筛选栏 -->
    <div class="card filter-bar">
      <el-form :inline="true" @submit.prevent="loadData">
        <el-form-item label="审核结论">
          <el-select v-model="filters.conclusion" placeholder="全部" clearable>
            <el-option label="通过" value="通过" />
            <el-option label="不通过" value="不通过" />
            <el-option label="转人工审核" value="转人工审核" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadData">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 审核日志表格 -->
    <div class="card">
      <el-table :data="logs" stripe v-loading="loading">
        <el-table-column prop="customer_name" label="客户" min-width="140" show-overflow-tooltip />
        <el-table-column prop="project_name" label="项目" min-width="160" show-overflow-tooltip />
        <el-table-column label="审核结论" width="110">
          <template #default="{ row }">
            <el-tag :type="conclusionType(row.conclusion)">{{ row.conclusion }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="region" label="区域" width="110" show-overflow-tooltip />
        <el-table-column prop="delivery_type" label="交付类型" width="90" />
        <el-table-column prop="project_type" label="项目类型" width="100" />
        <el-table-column label="规则详情" min-width="200">
          <template #default="{ row }">
            <div v-if="row.rules_result && row.rules_result.length">
              <span v-for="(r, i) in row.rules_result" :key="i" style="margin-right: 8px;">
                <el-tag size="small" :type="r.result === '通过' ? 'success' : r.result === '不通过' ? 'danger' : 'warning'">
                  R{{ r.rule_id }}: {{ r.result }}
                </el-tag>
              </span>
            </div>
            <span v-else-if="row.error" style="color: #f56c6c; font-size: 12px">{{ row.error }}</span>
            <span v-else style="color: #999">-</span>
          </template>
        </el-table-column>
        <el-table-column label="回访" width="90" align="center">
          <template #default="{ row }">
            <router-link v-if="row.conclusion === '通过'" to="/visit" style="font-size: 12px; color: #409eff; text-decoration: none;">
              <el-tag v-if="visitStatusMap[row.project_id] === 'completed'" type="success" size="small">已回访</el-tag>
              <el-tag v-else-if="visitStatusMap[row.project_id] === 'running'" type="warning" size="small">回访中</el-tag>
              <el-tag v-else-if="visitStatusMap[row.project_id] === 'failed' || visitStatusMap[row.project_id] === 'partial'" type="danger" size="small">失败</el-tag>
              <span v-else style="color: #409eff; cursor: pointer;">待回访</span>
            </router-link>
            <span v-else style="color: #999">-</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
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
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getAuditLogs, runReview, getPendingProjects, auditSingleProject } from '../api'

const logs = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 50
const loading = ref(false)
const running = ref(false)

const pendingProjects = ref<any[]>([])
const pendingLoading = ref(false)
const pendingVisible = ref(false)
const auditingMap = reactive<Record<string, boolean>>({})

// 回访状态缓存：project_id -> status（从 visit 页面数据加载）
const visitStatusMap = reactive<Record<string, string>>({})

const filters = ref({
  conclusion: '',
})

function resetFilters() {
  filters.value = { conclusion: '' }
  page.value = 1
  loadData()
}

function conclusionType(c: string) {
  if (c === '通过') return 'success'
  if (c === '不通过') return 'danger'
  if (c === '转人工审核') return 'warning'
  if (c === 'error') return 'info'
  return ''
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return iso.replace('T', ' ').slice(0, 19)
}

async function loadData() {
  loading.value = true
  try {
    const res: any = await getAuditLogs({
      conclusion: filters.value.conclusion || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    logs.value = res.items || []
    total.value = res.total || 0
    // 加载回访状态
    await loadVisitStatus()
  } catch {
    // silent
  } finally {
    loading.value = false
  }
}

async function loadVisitStatus() {
  // 批量查询已通过审核项目的回访状态
  const approved = logs.value.filter((r: any) => r.conclusion === '通过' && r.project_id)
  if (!approved.length) return
  try {
    const { getVisitLogs } = await import('../api')
    // 逐项目查询回访日志（简化方案：批量查询最近回访日志，然后按 project_id 匹配）
    const visitRes: any = await getVisitLogs({ limit: 200 })
    const visitItems: any[] = visitRes.items || []
    // 构建 project_id -> 最新 visit 状态映射
    const statusMap: Record<string, string> = {}
    for (const v of visitItems) {
      // 取最新的状态（列表已按 created_at desc 排序）
      if (!statusMap[v.project_id]) {
        statusMap[v.project_id] = v.status
      }
    }
    // 将回访状态写入 visitStatusMap
    for (const r of approved) {
      visitStatusMap[r.project_id] = statusMap[r.project_id] || ''
    }
  } catch {
    // 回访状态加载失败不影响主流程
  }
}

async function handleRun() {
  running.value = true
  try {
    const res: any = await runReview()
    if (res.status === 'skipped') {
      ElMessage.warning(res.reason || '审核未启用')
    } else {
      ElMessage.success(`审核完成: 通过${res.passed} 拒绝${res.rejected} 转人工${res.manual} 失败${res.errors}`)
      loadData()
    }
  } catch (e: any) {
    ElMessage.error('审核执行失败: ' + e.message)
  } finally {
    running.value = false
  }
}

async function loadPending() {
  pendingVisible.value = true
  pendingLoading.value = true
  try {
    const res: any = await getPendingProjects()
    pendingProjects.value = res.items || []
  } catch (e: any) {
    ElMessage.error('获取待审核项目失败: ' + e.message)
  } finally {
    pendingLoading.value = false
  }
}

async function handleAuditSingle(row: any) {
  if (auditingMap[row.project_id]) return
  auditingMap[row.project_id] = true
  try {
    const res: any = await auditSingleProject(row.project_id)
    if (res.conclusion === 'error') {
      ElMessage.error(`审核失败 (${row.project_name}): ${res.error || '未知错误'}`)
    } else {
      ElMessage.success(`${row.project_name}: ${res.conclusion}`)
    }
    // 刷新待审核列表和日志
    loadPending()
    loadData()
  } catch (e: any) {
    ElMessage.error(`审核失败 (${row.project_name}): ${e.message}`)
  } finally {
    auditingMap[row.project_id] = false
  }
}

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.audit-page {
  max-width: 1400px;
}
.action-bar {
  display: flex;
  gap: 12px;
  align-items: center;
}
.filter-bar {
  margin-top: 16px;
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
</style>
