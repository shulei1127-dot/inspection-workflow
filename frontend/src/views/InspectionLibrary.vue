<template>
  <div class="library-page">
    <div class="page-header">
      <h1>🗂️ 巡检信息库</h1>
      <div class="header-actions">
        <el-button type="primary" :loading="syncing" @click="handleSync">
          {{ syncing ? '同步中...' : '同步信息库' }}
        </el-button>
        <el-button type="warning" :loading="previewing" @click="handlePreview">
          {{ previewing ? '预览中...' : '回写预览' }}
        </el-button>
        <el-button type="success" :loading="backfilling" @click="handleBackfill">
          {{ backfilling ? '回写中...' : '执行回写' }}
        </el-button>
      </div>
    </div>

    <!-- 操作结果提示 -->
    <div v-if="lastResult" class="card" style="margin-bottom: 16px">
      <h3 style="margin-bottom: 8px">{{ lastResult.title }}</h3>
      <div style="display: flex; gap: 24px; flex-wrap: wrap">
        <span v-for="(v, k) in lastResult.stats" :key="k">{{ k }}: {{ v }}</span>
      </div>
      <el-alert v-if="lastResult.warning" :title="lastResult.warning" type="info" :closable="false" style="margin-top: 8px" />
    </div>

    <!-- 筛选 -->
    <div class="card filter-bar">
      <el-form :inline="true" @submit.prevent="loadData">
        <el-form-item label="客户名称">
          <el-input v-model="filters.customer_name" placeholder="模糊搜索" clearable style="width: 180px" />
        </el-form-item>
        <el-form-item label="产品名称">
          <el-input v-model="filters.product_name" placeholder="模糊搜索" clearable style="width: 180px" />
        </el-form-item>
        <el-form-item label="项目ID">
          <el-input v-model="filters.project_id" placeholder="精确匹配" clearable style="width: 180px" />
        </el-form-item>
        <el-form-item label="交付ID">
          <el-input v-model="filters.delivery_id" placeholder="精确匹配" clearable style="width: 180px" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="filters.missing_info">只看缺少地址/邮箱</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="loadData">查询</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- 列表 -->
    <div class="card">
      <div class="action-left" style="padding: 12px 16px 0">
        <span class="total-label">共 {{ total }} 条巡检信息</span>
        <span v-if="missingCount > 0" class="pending-label" style="margin-left: 12px">
          {{ missingCount }} 条缺少地址或邮箱
        </span>
      </div>
      <el-table :data="items" stripe style="width: 100%" v-loading="loading">
        <el-table-column prop="customer_name" label="客户名称" min-width="200" show-overflow-tooltip />
        <el-table-column prop="product_name" label="产品名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="contact_name" label="巡检联系人" width="110" />
        <el-table-column prop="contact_phone" label="联系电话" width="130" />
        <el-table-column prop="on_site_address" label="现场地址" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.on_site_address">{{ row.on_site_address }}</span>
            <el-tag v-else type="danger" size="small">缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="report_email" label="报告发送邮箱" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.report_email">{{ row.report_email }}</span>
            <el-tag v-else type="danger" size="small">缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="pts_order_id" label="工单ID" width="140">
          <template #default="{ row }">
            <a :href="`https://pts.chaitin.net/project/order/${row.pts_order_id}`" target="_blank" class="link">{{ row.pts_order_id }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="delivery_id" label="交付ID" width="130" show-overflow-tooltip />
        <el-table-column prop="project_id" label="项目ID" width="130" show-overflow-tooltip />
        <el-table-column prop="updated_at" label="更新时间" width="160" />
      </el-table>
      <div style="display: flex; justify-content: flex-end; padding: 12px 16px">
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

    <!-- 回写预览弹窗 -->
    <el-dialog v-model="previewVisible" title="回写预览（不会实际写入）" width="80%">
      <div v-if="previewResult">
        <el-alert
          :title="`检查 ${previewResult.checked} 条，可回写 ${previewResult.filled} 条，无来源 ${previewResult.no_source} 条`"
          type="info"
          :closable="false"
          style="margin-bottom: 12px"
        />
        <el-table :data="previewResult.preview" stripe max-height="480">
          <el-table-column prop="customer_name" label="客户名称" min-width="200" show-overflow-tooltip />
          <el-table-column prop="product_name" label="产品名称" min-width="160" show-overflow-tooltip />
          <el-table-column label="回写字段" width="150">
            <template #default="{ row }">
              <el-tag v-for="f in row.filled_fields" :key="f" size="small" style="margin-right: 4px">
                {{ f === 'on_site_address' ? '现场地址' : '报告邮箱' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="sources" label="来源工单" width="220">
            <template #default="{ row }">
              <span v-for="(v, k) in row.sources" :key="k" style="margin-right: 8px">
                <a :href="`https://pts.chaitin.net/project/order/${v}`" target="_blank" class="link">{{ v.slice(0, 8) }}</a>
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="address" label="将写入地址" min-width="200" show-overflow-tooltip />
          <el-table-column prop="email" label="将写入邮箱" min-width="200" show-overflow-tooltip />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
        <el-button type="success" :loading="backfilling" @click="handleBackfill">确认执行回写</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getInspectionLibrary, syncInspectionLibrary, backfillInspectionLibrary } from '../api'

const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const syncing = ref(false)
const previewing = ref(false)
const backfilling = ref(false)
const page = ref(1)
const missingCount = ref(0)

const lastResult = ref<{ title: string; stats: Record<string, any>; warning?: string } | null>(null)
const previewVisible = ref(false)
const previewResult = ref<any>(null)

const filters = reactive({
  customer_name: '',
  product_name: '',
  project_id: '',
  delivery_id: '',
  missing_info: false,
  limit: 50,
})

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, any> = {
      ...filters,
      offset: (page.value - 1) * filters.limit,
    }
    if (!params.missing_info) delete params.missing_info
    const res = await getInspectionLibrary(params)
    items.value = res.items || []
    total.value = res.total || 0
    if (!filters.missing_info) {
      const missing = await getInspectionLibrary({ ...params, missing_info: true, limit: 1 })
      missingCount.value = missing.total || 0
    } else {
      missingCount.value = total.value
    }
  } catch (e: any) {
    ElMessage.error('加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

function onPageChange(p: number) {
  page.value = p
  loadData()
}

function resetFilters() {
  filters.customer_name = ''
  filters.product_name = ''
  filters.project_id = ''
  filters.delivery_id = ''
  filters.missing_info = false
  page.value = 1
  loadData()
}

async function handleSync() {
  syncing.value = true
  try {
    const res = await syncInspectionLibrary()
    lastResult.value = {
      title: '✅ 同步完成',
      stats: {
        钉钉表记录: res.total,
        新增: res.new,
        已存在: res.updated,
        跳过: res.skipped,
      },
    }
    ElMessage.success('同步完成')
    loadData()
  } catch (e: any) {
    ElMessage.error('同步失败: ' + e.message)
  } finally {
    syncing.value = false
  }
}

async function handlePreview() {
  previewing.value = true
  try {
    previewResult.value = await backfillInspectionLibrary(true)
    previewVisible.value = true
  } catch (e: any) {
    ElMessage.error('预览失败: ' + e.message)
  } finally {
    previewing.value = false
  }
}

async function handleBackfill() {
  backfilling.value = true
  try {
    const res = await backfillInspectionLibrary(false)
    lastResult.value = {
      title: '✅ 回写完成',
      stats: {
        检查: res.checked,
        已回写: res.filled,
        无来源: res.no_source,
      },
      warning: res.filled === 0 ? '本次没有可回写的记录（可能都已填写或无同项目历史数据）' : undefined,
    }
    ElMessage.success(`回写完成，共 ${res.filled} 条`)
    previewVisible.value = false
    loadData()
  } catch (e: any) {
    ElMessage.error('回写失败: ' + e.message)
  } finally {
    backfilling.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.link {
  color: #409eff;
  text-decoration: none;
}
.link:hover {
  text-decoration: underline;
}
</style>
