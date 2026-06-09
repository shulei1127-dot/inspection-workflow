<template>
  <div class="email-tool-page">
    <div class="page-header">
      <h1>邮件发送</h1>
      <div style="display: flex; align-items: center; gap: 12px;">
        <el-tag v-if="form.recordId" type="success" size="small">来自 AITable: {{ form.customerName }}</el-tag>
        <el-button @click="showConfig = !showConfig">
          {{ showConfig ? '收起配置' : '发件人配置' }}
        </el-button>
      </div>
    </div>

    <!-- SMTP Config -->
    <div v-if="showConfig" class="card" style="margin-bottom: 16px">
      <h3>发件人配置</h3>
      <el-form label-width="100px" label-position="right">
        <el-form-item label="发件人邮箱">
          <el-input v-model="config.sender_email" placeholder="your@dingtalk.com" />
        </el-form-item>
        <el-form-item label="授权码">
          <el-input v-model="config.sender_password" type="password" show-password placeholder="在钉钉邮箱设置中生成授权码" />
        </el-form-item>
        <el-form-item label="显示名称">
          <el-input v-model="config.sender_name" />
        </el-form-item>
        <el-form-item label="SMTP 服务器">
          <el-input v-model="config.smtp_server" />
        </el-form-item>
        <el-form-item label="SMTP 端口">
          <el-input-number v-model="config.smtp_port" :min="1" :max="65535" :step="1" />
        </el-form-item>
        <el-form-item label="默认抄送">
          <el-input v-model="config.cc_emails" placeholder="多个邮箱用逗号分隔" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="saveConfig">保存配置</el-button>
        </el-form-item>
      </el-form>
    </div>

    <!-- Upload PDF -->
    <div class="card">
      <h3>上传巡检报告</h3>
      <p class="hint">上传 PDF 巡检报告（支持多个），AI 自动提取信息并发送邮件</p>
      <el-upload
        ref="uploadRef"
        :auto-upload="false"
        :on-change="handleFileChange"
        :on-remove="handleFileRemove"
        :file-list="fileList"
        accept=".pdf"
        multiple
      >
        <el-button type="primary">选择 PDF 文件</el-button>
      </el-upload>
      <div v-if="fileList.length" class="upload-info">
        <span>已上传 {{ fileList.length }} 个文件</span>
        <div class="upload-actions">
          <el-button type="success" :loading="extracting" @click="handleExtract">
            {{ extracting ? '分析中...' : '手动分析' }}
          </el-button>
          <el-button @click="handleReExtract" :disabled="extracting">重新提取</el-button>
          <el-button type="danger" @click="handleClear">清空</el-button>
        </div>
      </div>
    </div>

    <!-- AI extracted info -->
    <div class="card" style="margin-top: 16px">
      <h3>AI 提取结果</h3>
      <el-form label-width="100px" label-position="right">
        <el-form-item label="客户名称">
          <el-input v-model="form.customerName" />
        </el-form-item>
        <el-form-item label="产品名称">
          <el-input v-model="form.productName" />
        </el-form-item>
        <el-form-item label="巡检时间">
          <el-input v-model="form.inspectionDate" />
        </el-form-item>
        <el-form-item label="数量">
          <el-input v-model="form.quantity" />
        </el-form-item>
        <el-form-item label="收件人邮箱">
          <el-input v-model="form.recipientEmails" placeholder="输入邮箱或中文姓名，多个用逗号分隔" @blur="normalizeEmails" @input="userEditedEmails = true" />
          <div class="hint" style="margin-top: 4px">可直接输入中文姓名自动转为长亭邮箱，如：舒磊 → lei.shu@chaitin.com</div>
          <el-alert v-if="form.recipientEmails && !form.recipientEmails.includes('@')" type="warning" :closable="false" style="margin-top: 4px">
            未检测到有效邮箱，请手动添加
          </el-alert>
        </el-form-item>
        <el-form-item label="销售姓名">
          <el-input v-model="form.salesName" placeholder="输入中文姓名，自动转为长亭邮箱" @blur="handleSalesBlur" />
          <div class="hint" style="margin-top: 4px">销售邮箱将自动填入抄送栏</div>
        </el-form-item>
        <el-form-item label="抄送邮箱">
          <el-input v-model="form.ccEmails" placeholder="多个邮箱用逗号分隔" />
        </el-form-item>
      </el-form>

      <!-- Product summaries -->
      <div v-if="form.summaries.length" style="margin-top: 12px">
        <h4 style="margin-bottom: 8px; font-size: 14px; color: #666">各产品巡检总结</h4>
        <div v-for="(s, i) in form.summaries" :key="i" style="margin-bottom: 8px">
          <div style="font-size: 13px; color: #999; margin-bottom: 4px">{{ s.product }}</div>
          <el-input v-model="form.summaries[i].summary" type="textarea" :rows="3" />
        </div>
      </div>
    </div>

    <!-- Email content -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <h3>邮件内容</h3>
        <el-button size="small" @click="resetEmailContent">恢复默认</el-button>
      </div>
      <el-form label-width="100px" label-position="right">
        <el-form-item label="邮件标题">
          <el-input v-model="form.subject" />
        </el-form-item>
        <el-form-item label="邮件正文">
          <el-input v-model="form.body" type="textarea" :rows="10" />
        </el-form-item>
      </el-form>
    </div>

    <!-- Send -->
    <div class="card" style="margin-top: 16px">
      <div class="send-bar">
        <el-button type="primary" size="large" :loading="sending" @click="handleSend" :disabled="!config.sender_email || !config.sender_password">
          发送邮件
        </el-button>
        <el-button size="large" @click="handleReExtract" :disabled="extracting || !fileList.length">重新提取</el-button>
        <el-button size="large" type="danger" @click="handleClear">清空</el-button>
        <span v-if="sendResult" :style="{ marginLeft: '16px', fontWeight: 600, color: sendResult.success ? '#52c41a' : '#ff4d4f' }">
          {{ sendResult.message }}
        </span>
      </div>
    </div>

    <!-- Send history -->
    <div class="card" style="margin-top: 16px">
      <div class="card-header">
        <h3>发送记录</h3>
        <el-button size="small" @click="loadHistory">刷新</el-button>
      </div>
      <el-table v-if="history.length" :data="history" stripe size="small">
        <el-table-column prop="time" label="时间" width="170" />
        <el-table-column prop="customer" label="客户" min-width="160" show-overflow-tooltip />
        <el-table-column prop="product" label="产品" min-width="120" show-overflow-tooltip />
        <el-table-column prop="to" label="收件人" min-width="200" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === '成功' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="files" label="附件" width="80" align="center">
          <template #default="{ row }">
            {{ row.files > 1 ? row.files + '个' : '-' }}
          </template>
        </el-table-column>
      </el-table>
      <div v-else class="empty-hint">暂无发送记录</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { extractPdfInfo, convertNameToEmail, sendInspectionEmail, getEmailConfig, fetchAitableAttachments, reExtractPdfInfo, getPreAnalysisStatus } from '../api'
import type { UploadFile } from 'element-plus'
import { useRoute } from 'vue-router'

const route = useRoute()
const uploadRef = ref()
const fileList = ref<UploadFile[]>([])
const extracting = ref(false)
const sending = ref(false)
const fetchingAttachments = ref(false)
const sendResult = ref<{ success: boolean; message: string } | null>(null)
const showConfig = ref(false)
const history = ref<any[]>([])
const fromAitable = ref(false)
const fromPreAnalysis = ref(false)
const userEditedEmails = ref(false)

const config = reactive({
  sender_email: '',
  sender_password: '',
  sender_name: '长亭科技',
  smtp_server: 'smtpdm.aliyun.com',
  smtp_port: 465,
  cc_emails: '',
})

const form = reactive({
  customerName: '',
  productName: '',
  inspectionDate: '',
  quantity: '',
  recipientEmails: '',
  ccEmails: '',
  subject: '【长亭科技巡检报告】',
  body: `尊敬的客户，您好，

非常感谢对长亭科技的信任！本司于 {时间} 对贵司的 {数量} {产品名称} 进行了一次全面的巡检，结果如下：

{巡检总结}

详细巡检报告见附件，请查收！

后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～`,
  summaries: [] as { product: string; summary: string }[],
  fileIds: [] as string[],
  salesName: '',
  recordId: '',
})

onMounted(async () => {
  await loadConfig()
  loadHistory()

  // Pre-fill from route query params (navigated from Monitor email pending)
  const q = route.query
  if (q.customer_name || q.product_name || q.emails || q.sales_name) {
    form.customerName = (q.customer_name as string) || ''
    form.productName = (q.product_name as string) || ''

    // Build recipient emails from query
    const emails: string[] = []
    if (q.emails) {
      emails.push(...(q.emails as string).split(',').map(e => e.trim()).filter(Boolean))
    }
    form.recipientEmails = emails.join(', ')

    // Convert sales name to email
    if (q.sales_name) {
      const salesName = q.sales_name as string
      form.salesName = salesName
      // Try to convert Chinese name to email asynchronously
      convertNameToEmail(salesName).then(res => {
        if (res.email && res.email.includes('@')) {
          form.ccEmails = res.email
        }
      }).catch(() => {})
    }

    form.recordId = (q.record_id as string) || ''

    // Check if pre-analysis data exists for this record
    if (form.recordId && q.pre_analysis === 'true') {
      await loadAndApplyPreAnalysis(form.recordId)
    }

    // Generate email content with pre-filled data
    if (form.customerName) {
      generateEmailContent()
    }

    // Auto-fetch attachments from AITable if record_id is present
    // Skip AI extraction if pre-analysis already provided AI data
    if (form.recordId) {
      fetchAitableAttachmentsForRecord(form.recordId)
    }
  }
})

async function loadAndApplyPreAnalysis(recordId: string) {
  try {
    const res = await getPreAnalysisStatus()
    const pa = res.pre_analysis?.[recordId]
    if (pa && pa.analysis_status === 'success') {
      fromPreAnalysis.value = true
      // Apply pre-analyzed AI data to form (overrides query params)
      if (pa.customer_name) form.customerName = pa.customer_name
      if (pa.product_name) form.productName = pa.product_name
      if (pa.inspection_date) form.inspectionDate = pa.inspection_date
      if (pa.quantity) form.quantity = pa.quantity
      if (pa.emails && !userEditedEmails.value) form.recipientEmails = pa.emails
      if (pa.summaries && pa.summaries.length > 0) {
        form.summaries = pa.summaries
      } else if (pa.summary) {
        form.summaries = [{
          product: form.productName || '产品',
          summary: pa.summary,
        }]
      }
      generateEmailContent()
      ElMessage.success('已加载预分析数据，无需重新 AI 分析')
    } else if (pa && pa.analysis_status === 'failed') {
      ElMessage.warning('预分析失败，将重新进行 AI 分析: ' + (pa.error_message || ''))
    }
  } catch {
    // If pre-analysis API fails, fall back to normal flow
  }
}

async function fetchAitableAttachmentsForRecord(recordId: string) {
  fetchingAttachments.value = true
  const msg = ElMessage({ message: '正在从钉钉文档获取巡检报告附件，请稍候...', type: 'info', duration: 0 })
  try {
    const res = await fetchAitableAttachments(recordId)
    msg.close()
    if (!res.success) {
      ElMessage.warning(res.message || '获取附件失败')
      return
    }

    // Mark as AITable-sourced files (no raw upload data)
    fromAitable.value = true

    // Store file IDs for sending
    form.fileIds = res.file_ids || []

    // Update form with server-returned data (more accurate than query params)
    if (res.customer_name) form.customerName = res.customer_name
    if (res.product_name) form.productName = res.product_name
    if (res.email_address_str && !userEditedEmails.value) form.recipientEmails = res.email_address_str
    if (res.sales_name) form.salesName = res.sales_name

    // Convert sales name to CC email
    if (res.sales_name) {
      try {
        const emailRes = await convertNameToEmail(res.sales_name)
        if (emailRes.email && emailRes.email.includes('@')) {
          form.ccEmails = emailRes.email
        }
      } catch {}
    }

    // Apply AI extraction result if available (skip if pre-analysis already provided data)
    if (!fromPreAnalysis.value && res.ai_info) {
      const info = res.ai_info
      if (info.inspection_date) form.inspectionDate = info.inspection_date
      if (info.quantity) form.quantity = info.quantity

      // Multi-product summaries from backend
      if (res.summaries && res.summaries.length > 0) {
        form.summaries = res.summaries
      } else if (info.summary) {
        form.summaries = [{
          product: form.productName || '产品',
          summary: info.summary,
        }]
      }
    }

    if (res.ai_error) {
      ElMessage.warning('AI 分析: ' + res.ai_error)
    }

    // Update file list display
    if (res.filenames && res.filenames.length > 0) {
      fileList.value = res.filenames.map((name: string, i: number) => ({
        name,
        status: 'success',
        uid: Date.now() + i,
        size: 0,
      }))
    }

    generateEmailContent()
    ElMessage.success(`已获取 ${res.file_ids?.length || 0} 个附件`)
  } catch (e: any) {
    msg.close()
    ElMessage.error('获取附件失败: ' + e.message)
  } finally {
    fetchingAttachments.value = false
  }
}

async function loadConfig() {
  try {
    const res = await getEmailConfig()
    config.sender_email = res.sender_email || ''
    config.sender_name = res.sender_name || '长亭科技'
    config.smtp_server = res.smtp_server || 'smtpdm.aliyun.com'
    config.smtp_port = res.smtp_port || 465
    config.cc_emails = res.cc_emails || ''
    // Password not sent to frontend; just track if it exists
    if (res.has_password) {
      config.sender_password = '••••••••'
    }
  } catch {
    // Use defaults
  }
}

function saveConfig() {
  // Save to backend
  const data = { ...config }
  if (data.sender_password === '••••••••') {
    delete (data as any).sender_password // Don't overwrite if unchanged
  }
  fetch('/api/email-tool/save-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(r => r.json()).then(res => {
    if (res.success) {
      ElMessage.success('配置已保存')
      showConfig.value = false
    } else {
      ElMessage.error(res.message || '保存失败')
    }
  }).catch(() => {
    ElMessage.error('保存失败')
  })
}

function loadHistory() {
  fetch('/api/email-tool/history').then(r => r.json()).then(res => {
    history.value = res.history || []
  }).catch(() => {
    history.value = []
  })
}

function handleFileChange(_file: UploadFile, list: UploadFile[]) {
  fileList.value = list
}

function handleFileRemove(_file: UploadFile, list: UploadFile[]) {
  fileList.value = list
}

async function handleExtract() {
  // AITable-sourced files: use re-extract with file_ids
  if (fromAitable.value && form.fileIds.length > 0) {
    extracting.value = true
    sendResult.value = null
    try {
      const res = await reExtractPdfInfo(form.fileIds.join(','))
      if (!res.files || res.files.length === 0) {
        ElMessage.error('未提取到任何信息')
        return
      }

      const aiErrors = res.files.filter((f: any) => f.ai_error).map((f: any) => `${f.filename}: ${f.ai_error}`)
      if (aiErrors.length > 0) {
        ElMessage.warning('AI 分析失败: ' + aiErrors.join('; '))
      }

      applyExtractResult(res)
      if (aiErrors.length === 0) {
        ElMessage.success(`分析完成：${res.files.length} 个文件`)
      }
    } catch (e: any) {
      ElMessage.error('分析失败: ' + e.message)
    } finally {
      extracting.value = false
    }
    return
  }

  // Manual upload: use extract with FormData
  if (!fileList.value.length) {
    ElMessage.warning('请先选择 PDF 文件')
    return
  }

  extracting.value = true
  sendResult.value = null
  try {
    const formData = new FormData()
    for (const f of fileList.value) {
      if (f.raw) {
        formData.append('files', f.raw)
      }
    }

    const res = await extractPdfInfo(formData)
    if (!res.files || res.files.length === 0) {
      ElMessage.error('未提取到任何信息')
      return
    }

    // Check for AI errors
    const aiErrors = res.files.filter((f: any) => f.ai_error).map((f: any) => `${f.filename}: ${f.ai_error}`)
    if (aiErrors.length > 0) {
      ElMessage.warning('AI 分析失败: ' + aiErrors.join('; '))
    }

    applyExtractResult(res)
    if (aiErrors.length === 0) {
      ElMessage.success(`分析完成：${res.files.length} 个文件`)
    }
  } catch (e: any) {
    ElMessage.error('分析失败: ' + e.message)
  } finally {
    extracting.value = false
  }
}

function applyExtractResult(res: any) {
  form.fileIds = res.files.map((f: any) => f.file_id)

  const firstInfo = res.files[0].info
  form.customerName = firstInfo.customer_name || ''
  form.inspectionDate = firstInfo.inspection_date || ''

  const productNames = res.files.map((f: any) => f.info?.product_name || '').filter((n: string) => n)
  form.productName = productNames.join('、')

  const quantities = res.files
    .map((f: any) => {
      const qty = f.info?.quantity || ''
      const prod = f.info?.product_name || ''
      return qty && prod ? `${qty}${prod}` : prod
    })
    .filter((q: string) => q)
  form.quantity = quantities.join('、')

  const allEmails: string[] = res.all_emails || []
  if (!userEditedEmails.value) form.recipientEmails = allEmails.join(', ')

  form.summaries = res.files.map((f: any) => ({
    product: f.info?.product_name || '产品',
    summary: f.info?.summary || '',
  }))

  // Merge default CC with existing sales CC (don't overwrite)
  if (config.cc_emails && !form.ccEmails.includes(config.cc_emails)) {
    form.ccEmails = [config.cc_emails, form.ccEmails].filter(Boolean).join(', ')
  }

  generateEmailContent()
}

async function handleReExtract() {
  if (!fileList.value.length) return
  await handleExtract()
}

function generateEmailContent() {
  let combinedSummary = ''
  if (form.summaries.length === 1) {
    combinedSummary = form.summaries[0].summary
  } else {
    combinedSummary = form.summaries
      .filter(s => s.summary)
      .map(s => `【${s.product}】\n${s.summary}`)
      .join('\n\n')
  }

  // Short product name for subject: "下一代Web应用防火墙（雷池20系列）" → "雷池"
  const productShortMap: Record<string, string> = {
    '雷池': '雷池', '下一代Web应用防火墙': '雷池', '下一代 Web 应用防火墙': '雷池',
    '洞鉴': '洞鉴', '牧云': '牧云', '云工作负载保护平台': '牧云',
    '谛听': '谛听', '万象': '万象',
  }
  const productKeywords = ['雷池', '洞鉴', '谛听', '牧云', '万象']
  let shortProduct = form.productName || ''
  for (const kw of productKeywords) {
    if (shortProduct.includes(kw)) { shortProduct = kw; break }
  }
  if (!productKeywords.includes(shortProduct)) {
    for (const [prefix, short] of Object.entries(productShortMap)) {
      if (shortProduct.startsWith(prefix) || shortProduct.includes(prefix)) { shortProduct = short; break }
    }
  }

  const dateDisplay = (form.inspectionDate || '').replace(/-/g, '.')

  form.subject = form.customerName
    ? `【长亭科技巡检报告】${form.customerName}${shortProduct}巡检报告-${dateDisplay}`
    : '【长亭科技巡检报告】'

  form.body = `尊敬的客户，您好，

非常感谢对长亭科技的信任！本司于 ${form.inspectionDate || '{时间}'} 对贵司的 ${form.quantity || '{数量}'} 进行了一次全面的巡检，结果如下：

${combinedSummary || '{巡检总结}'}

详细巡检报告见附件，请查收！

后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～`
}

function resetEmailContent() {
  generateEmailContent()
}

async function normalizeEmails() {
  const input = form.recipientEmails.trim()
  if (!input) return

  const tokens = input.split(/[,，;；]+/).map(t => t.trim()).filter(Boolean)
  const normalized: string[] = []

  for (const token of tokens) {
    if (token.includes('@')) {
      normalized.push(token)
    } else if (/[\u4e00-\u9fff]/.test(token)) {
      try {
        const res = await convertNameToEmail(token)
        normalized.push(res.email || token)
      } catch {
        normalized.push(token)
      }
    } else {
      normalized.push(token)
    }
  }

  form.recipientEmails = normalized.join(', ')
}

async function handleSalesBlur() {
  const name = form.salesName.trim()
  if (!name) return

  try {
    const res = await convertNameToEmail(name)
    if (res.email && res.email.includes('@')) {
      form.ccEmails = res.email
      ElMessage.success(`销售邮箱: ${res.email}`)
    }
  } catch {
    // Ignore conversion errors
  }
}

async function handleSend() {
  if (!config.sender_email) {
    ElMessage.error('请先配置发件人邮箱')
    showConfig.value = true
    return
  }
  if (!config.sender_password || config.sender_password === '••••••••') {
    // Need real password
  }
  if (!form.recipientEmails.trim()) {
    ElMessage.error('请填写收件人邮箱')
    return
  }
  if (!form.customerName || !form.productName) {
    ElMessage.error('请填写客户名称和产品名称')
    return
  }

  sending.value = true
  sendResult.value = null
  try {
    const res = await sendInspectionEmail({
      to_emails: form.recipientEmails,
      subject: form.subject,
      body: form.body,
      file_ids: form.fileIds.join(','),
      cc_emails: [config.cc_emails, form.ccEmails].filter(Boolean).join(','),
      record_id: form.recordId,
      customer: form.customerName,
      product: form.productName,
    })
    sendResult.value = { success: res.success, message: res.message }
    if (res.success) {
      ElMessage.success('邮件发送成功')
      if (res.closure) {
        if (res.closure.success) {
          ElMessage.success('工单已自动闭环: ' + res.closure.message)
        } else {
          ElMessage.warning('工单闭环未成功: ' + res.closure.message)
        }
      }
      loadHistory()
    } else {
      ElMessage.error(res.message || '发送失败')
    }
  } catch (e: any) {
    sendResult.value = { success: false, message: e.message }
    ElMessage.error('发送失败: ' + e.message)
  } finally {
    sending.value = false
  }
}

function handleClear() {
  fileList.value = []
  sendResult.value = null
  fromAitable.value = false
  userEditedEmails.value = false
  form.customerName = ''
  form.productName = ''
  form.inspectionDate = ''
  form.quantity = ''
  form.recipientEmails = ''
  form.ccEmails = ''
  form.subject = '【长亭科技巡检报告】'
  form.body = `尊敬的客户，您好，\n\n非常感谢对长亭科技的信任！本司于 {时间} 对贵司的 {数量} 进行了一次全面的巡检，结果如下：\n\n{巡检总结}\n\n详细巡检报告见附件，请查收！\n\n后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～`
  form.summaries = []
  form.fileIds = []
  form.salesName = ''
  form.recordId = ''
}
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

.hint {
  font-size: 13px;
  color: #999;
  margin-bottom: 12px;
}

.upload-info {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: #409eff;
}

.upload-actions {
  display: flex;
  gap: 8px;
}

.send-bar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.empty-hint {
  text-align: center;
  color: #999;
  padding: 24px 0;
  font-size: 13px;
}
</style>
