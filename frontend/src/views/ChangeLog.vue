<template>
  <div class="page-container">
    <div class="card">
      <div class="card-header">
        <h3>变更知识库</h3>
        <div style="display: flex; gap: 8px; align-items: center">
          <el-date-picker v-model="filterDate" type="date" placeholder="选择日期" value-format="YYYY-MM-DD" size="small" style="width: 150px" />
          <el-select v-model="filterCategory" placeholder="分类" size="small" style="width: 130px" clearable>
            <el-option label="Bug 修复" value="bug_fix" />
            <el-option label="功能优化" value="feature_opt" />
            <el-option label="重要变更" value="important_change" />
            <el-option label="其他" value="other" />
          </el-select>
          <el-input v-model="filterKeyword" placeholder="关键词搜索" size="small" style="width: 150px" clearable />
          <el-button size="small" @click="handleSearch">查询</el-button>
        </div>
      </div>

      <div style="margin-bottom: 12px; display: flex; gap: 8px; align-items: center">
        <el-button type="primary" size="small" @click="showAddDialog = true">新增记录</el-button>
        <el-button size="small" @click="handleCollect">补采 Git 变更</el-button>
        <el-button type="warning" size="small" @click="handlePush">推送今日日报</el-button>
        <span v-if="selectedDate" style="margin-left: 8px; font-size: 13px; color: #666">当前操作日期：{{ selectedDate }}</span>
      </div>

      <el-table :data="items" stripe size="small" v-loading="loading">
        <el-table-column prop="change_date" label="日期" width="110" />
        <el-table-column prop="category" label="分类" width="100">
          <template #default="{ row }">
            <el-tag :type="categoryTagType(row.category)" size="small">{{ categoryLabel(row.category) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="title" label="标题" min-width="300" show-overflow-tooltip />
        <el-table-column prop="source_type" label="来源" width="90">
          <template #default="{ row }">
            <span>{{ row.source_type === 'git_commit' ? 'Git' : '手工' }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="author" label="作者" width="100" show-overflow-tooltip />
        <el-table-column prop="pushed_to_dingtalk" label="已推送" width="70">
          <template #default="{ row }">
            <el-tag :type="row.pushed_to_dingtalk ? 'success' : 'info'" size="small">{{ row.pushed_to_dingtalk ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button v-if="row.source_type === 'manual'" type="danger" size="small" link @click="handleDelete(row)">删除</el-button>
            <el-button v-if="row.detail" size="small" link @click="viewDetail = row.detail; viewDetailVisible = true">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top: 12px; display: flex; justify-content: flex-end">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          small
          @current-change="loadData"
          @size-change="handlePageSizeChange"
        />
      </div>
    </div>

    <!-- Add entry dialog -->
    <el-dialog v-model="showAddDialog" title="新增变更记录" width="500px">
      <el-form label-width="80px" size="small">
        <el-form-item label="变更日期">
          <el-date-picker v-model="addForm.change_date" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" style="width: 100%" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="addForm.category" style="width: 100%">
            <el-option label="Bug 修复" value="bug_fix" />
            <el-option label="功能优化" value="feature_opt" />
            <el-option label="重要变更" value="important_change" />
            <el-option label="其他" value="other" />
          </el-select>
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="addForm.title" placeholder="一句话描述变更内容" />
        </el-form-item>
        <el-form-item label="详细说明">
          <el-input v-model="addForm.detail" type="textarea" :rows="3" placeholder="可选，补充说明" />
        </el-form-item>
        <el-form-item label="记录人">
          <el-input v-model="addForm.author" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showAddDialog = false">取消</el-button>
        <el-button type="primary" @click="handleAdd">确认</el-button>
      </template>
    </el-dialog>

    <!-- Detail dialog -->
    <el-dialog v-model="viewDetailVisible" title="变更详情" width="500px">
      <div style="white-space: pre-wrap; font-size: 14px; line-height: 1.8">{{ viewDetail }}</div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getChangeLogs, createChangeLog, deleteChangeLog, collectChangeLogs, pushDailyChangeLogs } from '../api'

const items = ref<any[]>([])
const loading = ref(false)
const filterDate = ref('')
const filterCategory = ref('')
const filterKeyword = ref('')
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)

const showAddDialog = ref(false)
const addForm = reactive({
  change_date: '',
  category: 'bug_fix',
  title: '',
  detail: '',
  author: '',
})

const viewDetailVisible = ref(false)
const viewDetail = ref('')

const selectedDate = computed(() => filterDate.value || new Date().toISOString().slice(0, 10))

function categoryLabel(cat: string) {
  const map: Record<string, string> = { bug_fix: 'Bug 修复', feature_opt: '功能优化', important_change: '重要变更', other: '其他' }
  return map[cat] || cat
}

function categoryTagType(cat: string) {
  const map: Record<string, string> = { bug_fix: 'danger', feature_opt: 'success', important_change: 'warning', other: 'info' }
  return map[cat] || 'info'
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, any> = {
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    }
    if (filterDate.value) params.date_from = filterDate.value
    if (filterDate.value) params.date_to = filterDate.value
    if (filterCategory.value) params.category = filterCategory.value
    if (filterKeyword.value) params.keyword = filterKeyword.value
    const res = await getChangeLogs(params)
    items.value = res.items || []
    total.value = res.total || 0
  } catch {
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadData()
}

function handlePageSizeChange() {
  page.value = 1
  loadData()
}

async function handleAdd() {
  if (!addForm.change_date || !addForm.title) {
    ElMessage.warning('请填写日期和标题')
    return
  }
  try {
    await createChangeLog(addForm)
    ElMessage.success('添加成功')
    showAddDialog.value = false
    addForm.title = ''
    addForm.detail = ''
    addForm.author = ''
    loadData()
  } catch (e: any) {
    ElMessage.error('添加失败: ' + e.message)
  }
}

async function handleDelete(row: any) {
  try {
    await ElMessageBox.confirm('确认删除该记录？', '删除确认', { type: 'warning' })
    await deleteChangeLog(row.id)
    ElMessage.success('已删除')
    loadData()
  } catch { /* cancelled */ }
}

async function handleCollect() {
  try {
    const res = await collectChangeLogs(selectedDate.value)
    const r = res.result || {}
    ElMessage.success(`采集完成：新增 ${r.collected || 0} 条，跳过 ${r.skipped || 0} 条`)
    loadData()
  } catch (e: any) {
    ElMessage.error('采集失败: ' + e.message)
  }
}

async function handlePush() {
  try {
    await ElMessageBox.confirm(
      `确认推送 ${selectedDate.value} 的日报到钉钉？`,
      '推送确认',
      { confirmButtonText: '推送', cancelButtonText: '取消', type: 'warning' },
    )
  } catch { return }

  try {
    const res = await pushDailyChangeLogs(selectedDate.value)
    const r = res.result || {}
    if (r.pushed) {
      ElMessage.success(r.message || '推送成功')
    } else {
      ElMessage.warning(r.message || '推送未成功')
    }
    loadData()
  } catch (e: any) {
    ElMessage.error('推送失败: ' + e.message)
  }
}

onMounted(() => {
  loadData()
})
</script>

<style scoped>
.page-container {
  padding: 20px;
}
.card {
  background: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.card-header h3 {
  font-size: 16px;
  font-weight: 600;
}
</style>
