<template>
  <div class="change-log-page">
    <div class="page-header">
      <h1>📚 知识库变更</h1>
      <div class="header-actions">
        <el-date-picker v-model="selectedDate" type="date" placeholder="选择日期" value-format="YYYY-MM-DD"
          style="width: 160px; margin-right: 8px" />
        <el-select v-model="filterCategory" placeholder="分类筛选" clearable style="width: 140px; margin-right: 8px">
          <el-option label="Bug修复" value="bug_fix" />
          <el-option label="功能优化" value="feature_opt" />
          <el-option label="重要变更" value="important_change" />
          <el-option label="其他" value="other" />
        </el-select>
        <el-input v-model="keyword" placeholder="搜索标题/详情" clearable style="width: 180px; margin-right: 8px" />
        <el-button type="primary" @click="fetchLogs">查询</el-button>
        <el-button @click="showAddDialog = true">手动添加</el-button>
        <el-button type="warning" @click="handleCollect" :loading="collecting">采集Git</el-button>
        <el-button type="success" @click="handlePush" :loading="pushing">推送钉钉</el-button>
      </div>
    </div>

    <!-- 每日摘要 -->
    <div v-if="summary" class="card" style="margin-bottom: 16px">
      <h3 style="margin-bottom: 8px">{{ summary.date }} 变更摘要</h3>
      <div style="display: flex; gap: 24px; flex-wrap: wrap">
        <span>🐛 Bug修复: {{ summary.bug_fixes?.length || 0 }}</span>
        <span>✨ 功能优化: {{ summary.features?.length || 0 }}</span>
        <span>📌 重要变更: {{ summary.important?.length || 0 }}</span>
        <span>📝 其他: {{ summary.others?.length || 0 }}</span>
        <span>📊 总计: {{ summary.total_entries || 0 }}</span>
        <span>📬 已推送: {{ summary.pushed_count || 0 }}</span>
      </div>
    </div>

    <!-- 变更列表 -->
    <div class="card">
      <el-table :data="logs" stripe style="width: 100%">
        <el-table-column prop="change_date" label="日期" width="110" />
        <el-table-column prop="source_type" label="来源" width="100">
          <template #default="{ row }">
            <el-tag :type="row.source_type === 'manual' ? 'warning' : 'info'" size="small">
              {{ row.source_type === 'manual' ? '手动' : 'Git' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="category" label="分类" width="110">
          <template #default="{ row }">
            <el-tag :type="categoryTagType(row.category)" size="small">{{ categoryLabel(row.category) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="300" />
        <el-table-column prop="author" label="作者" width="120" />
        <el-table-column prop="git_hash" label="Hash" width="80">
          <template #default="{ row }">
            <span v-if="row.git_hash" class="git-hash">{{ row.git_hash.slice(0, 7) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="pushed_to_dingtalk" label="推送" width="70" align="center">
          <template #default="{ row }">
            <span v-if="row.pushed_to_dingtalk">✅</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70" align="center">
          <template #default="{ row }">
            <el-button v-if="row.source_type === 'manual'" type="danger" size="small" link @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top: 12px; display: flex; justify-content: space-between; align-items: center">
        <span style="font-size: 13px; color: #8c8c8c">共 {{ total }} 条</span>
        <el-pagination v-model:current-page="page" :page-size="limit" :total="total" layout="prev, pager, next"
          @current-change="fetchLogs" />
      </div>
    </div>

    <!-- 手动添加对话框 -->
    <el-dialog v-model="showAddDialog" title="手动添加变更记录" width="480px">
      <el-form :model="addForm" label-width="80px">
        <el-form-item label="日期">
          <el-date-picker v-model="addForm.change_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="addForm.category" style="width: 100%">
            <el-option label="Bug修复" value="bug_fix" />
            <el-option label="功能优化" value="feature_opt" />
            <el-option label="重要变更" value="important_change" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="addForm.title" />
        </el-form-item>
        <el-form-item label="详情">
          <el-input v-model="addForm.detail" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="作者">
          <el-input v-model="addForm.author" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showAddDialog = false">取消</el-button>
        <el-button type="primary" @click="handleAdd" :loading="adding">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getChangeLogs, createChangeLog, deleteChangeLog, collectChangeLogs, pushDailyChangeLogs, getChangeLogSummary } from '../api'

const logs = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const limit = ref(50)
const selectedDate = ref(new Date().toISOString().slice(0, 10))
const filterCategory = ref('')
const keyword = ref('')
const summary = ref<any>(null)
const showAddDialog = ref(false)
const adding = ref(false)
const collecting = ref(false)
const pushing = ref(false)

const addForm = ref({
  change_date: new Date().toISOString().slice(0, 10),
  category: 'feature_opt',
  title: '',
  detail: '',
  author: '',
})

const categoryLabel = (c: string) => {
  const map: Record<string, string> = { bug_fix: 'Bug修复', feature_opt: '功能优化', important_change: '重要变更', other: '其他' }
  return map[c] || c
}

const categoryTagType = (c: string) => {
  const map: Record<string, string> = { bug_fix: 'danger', feature_opt: 'success', important_change: 'warning', other: 'info' }
  return map[c] || 'info'
}

async function fetchLogs() {
  try {
    const params: Record<string, any> = { limit: limit.value, offset: (page.value - 1) * limit.value }
    if (selectedDate.value) params.date_from = selectedDate.value
    if (selectedDate.value) params.date_to = selectedDate.value
    if (filterCategory.value) params.category = filterCategory.value
    if (keyword.value) params.keyword = keyword.value
    const res = await getChangeLogs(params)
    logs.value = res.items || []
    total.value = res.total || 0
  } catch (e: any) {
    ElMessage.error('加载失败: ' + e.message)
  }
}

async function fetchSummary() {
  if (!selectedDate.value) return
  try {
    const res = await getChangeLogSummary(selectedDate.value)
    summary.value = res.result || null
  } catch {
    summary.value = null
  }
}

async function handleAdd() {
  adding.value = true
  try {
    await createChangeLog(addForm.value)
    ElMessage.success('添加成功')
    showAddDialog.value = false
    addForm.value = { change_date: new Date().toISOString().slice(0, 10), category: 'feature_opt', title: '', detail: '', author: '' }
    fetchLogs()
    fetchSummary()
  } catch (e: any) {
    ElMessage.error('添加失败: ' + e.message)
  } finally {
    adding.value = false
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm('确认删除该条变更记录？', '删除确认', { type: 'warning' })
    await deleteChangeLog(row.id)
    ElMessage.success('已删除')
    fetchLogs()
    fetchSummary()
  } catch {}
}

async function handleCollect() {
  if (!selectedDate.value) { ElMessage.warning('请先选择日期'); return }
  collecting.value = true
  try {
    await collectChangeLogs(selectedDate.value)
    ElMessage.success('采集完成')
    fetchLogs()
    fetchSummary()
  } catch (e: any) {
    ElMessage.error('采集失败: ' + e.message)
  } finally {
    collecting.value = false
  }
}

async function handlePush() {
  if (!selectedDate.value) { ElMessage.warning('请先选择日期'); return }
  pushing.value = true
  try {
    const res = await pushDailyChangeLogs(selectedDate.value)
    ElMessage.success(res.result?.message || '推送完成')
    fetchLogs()
    fetchSummary()
  } catch (e: any) {
    ElMessage.error('推送失败: ' + e.message)
  } finally {
    pushing.value = false
  }
}

onMounted(() => {
  fetchLogs()
  fetchSummary()
})
</script>

<style scoped>
.git-hash {
  font-family: 'SF Mono', Consolas, monospace;
  font-size: 12px;
  color: #8c8c8c;
}
</style>
