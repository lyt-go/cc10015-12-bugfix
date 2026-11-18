<template>
  <section class="page" data-module="data_export">
    <header class="page-head">
      <div><h2>数据导出</h2><p class="page-desc">按实验范围裁剪导出任务与文件清单，字段清单、实验归属与指标口径随文件一并留档。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记导出任务</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>
    <div class="scope-bar">
      <label><span>允许查看的实验范围</span>
        <input v-model="scope" placeholder="如 EXPE-0001，留空表示不裁剪" data-testid="scope-input" />
      </label>
      <button class="btn" type="button" data-testid="scope-apply" @click="reload">按范围重新查看</button>
      <span class="scope-hint">跨范围内容只读且不会出现在下载文件中</span>
    </div>
    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
      <div style="margin-top:4px;">实验范围：{{ (selectedItem.实验范围 || []).join('、') || '未声明' }} · 字段清单：{{ (selectedItem.字段清单 || []).join('、') || '未声明' }}</div>
    </div>

    <section class="manifest-panel" data-testid="manifest-workbench">
      <header class="manifest-head">
        <div>
          <h3>导出文件清单（V012）</h3>
          <p class="page-desc">清单生成是一条事务：异常时按 V012 回滚并清理残留；已发布文件只允许续写缺失分片。</p>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="refresh-manifests" @click="loadManifests">刷新清单</button>
        </div>
      </header>
      <div class="filter-bar">
        <label><span>导出任务</span>
          <select v-model="manifestTask" data-testid="manifest-task">
            <option v-for="item in allItems" :key="item.id" :value="String(item.id)">{{ item.name }}</option>
          </select>
        </label>
        <label><span>导出批次</span><input v-model="manifestBatch" data-testid="manifest-batch" placeholder="如 BATCH-0001" /></label>
        <label><span>拆分策略</span>
          <select v-model="manifestStrategy" data-testid="manifest-strategy">
            <option v-for="item in strategies" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <button class="btn primary" type="button" data-testid="generate-manifest" @click="generateManifest">生成清单</button>
      </div>
      <p v-if="manifestMessage" :class="manifestOk ? 'manifest-tip' : 'error-text'" data-testid="manifest-message">{{ manifestMessage }}</p>

      <div v-if="manifests.length" class="manifest-list">
        <article v-for="item in manifests" :key="item.导出批次" class="manifest-card" :data-testid="`manifest-${item.导出批次}`">
          <header class="manifest-card-head">
            <strong>{{ item.清单编号 }} · 批次 {{ item.导出批次 }}</strong>
            <span class="badge">{{ item.清单版本 }}</span>
            <span class="badge">{{ item.拆分策略 }}</span>
            <span class="badge">{{ item.发布状态 }}</span>
          </header>
          <div class="manifest-meta">
            <span>允许范围：{{ (item.可见范围 || item.允许范围 || []).join('、') }}</span>
            <span>记录 {{ item.记录总数 }} 条</span>
            <span v-if="item.排除范围 && item.排除范围.length">排除范围：{{ item.排除范围.join('、') }}</span>
            <span v-if="item.版本冲突">同批次多范围：{{ item.冲突处理 }}</span>
          </div>
          <table class="data-table manifest-files">
            <thead><tr><th>文件</th><th>实验范围</th><th>记录数</th><th>分片</th><th>范围分区</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="file in item.文件清单" :key="file.文件名">
                <td>{{ file.文件名 }}</td>
                <td>{{ file.实验范围.join('、') }}</td>
                <td>{{ file.记录数 }}</td>
                <td>
                  <span v-for="shard in file.分片" :key="shard.序号" :class="shard.状态 === '已写入' ? 'shard-ok' : 'shard-missing'">
                    #{{ shard.序号 }} {{ shard.状态 }}
                  </span>
                </td>
                <td>
                  <span v-for="part in file.范围分区" :key="part.实验范围">
                    {{ part.实验范围 }}[{{ part.起始记录 }}-{{ part.结束记录 }}]
                  </span>
                </td>
                <td class="row-actions">
                  <button class="link" type="button" @click="showManifest(item.导出批次)">详情</button>
                  <button class="link" type="button" @click="verify(item.导出批次)">核对</button>
                  <button class="link" type="button" @click="downloadFile(item.导出批次, file.文件名)">下载</button>
                  <button class="link" type="button" @click="interrupt(item.导出批次, file.文件名)">中断首分片</button>
                  <button class="link" type="button" @click="resume(item.导出批次)">续写</button>
                </td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>
      <p v-else class="empty-state">暂无导出文件清单，可先登记任务再生成清单</p>

      <div v-if="manifestDetail" class="manifest-detail" data-testid="manifest-detail">
        <header class="manifest-card-head">
          <strong>清单详情 {{ manifestDetail.清单编号 }}</strong>
          <button class="link" type="button" @click="manifestDetail = null">收起</button>
        </header>
        <div class="manifest-meta">
          <span>清单版本：{{ manifestDetail.清单版本 }}</span>
          <span>事务：{{ manifestDetail.事务?.状态 }}</span>
          <span>字段清单：{{ fieldNames.join('、') }}</span>
        </div>
        <table class="data-table">
          <thead><tr><th>指标键</th><th>字段标签</th><th>精度位数</th><th>口径版本</th></tr></thead>
          <tbody>
            <tr v-for="caliber in manifestDetail.指标口径" :key="caliber.指标键">
              <td>{{ caliber.指标键 }}</td><td>{{ caliber.字段标签 }}</td><td>{{ caliber.精度位数 }}</td><td>{{ caliber.口径版本 ?? '—' }}</td>
            </tr>
            <tr v-if="!manifestDetail.指标口径.length"><td colspan="4" class="empty-state">清单未声明指标口径</td></tr>
          </tbody>
        </table>
        <div v-if="manifestDetail.续写记录?.length" class="manifest-meta">
          <span v-for="(log, i) in manifestDetail.续写记录" :key="i">{{ log.时间 }} {{ log.文件 }} #{{ log.分片 }} {{ log.说明 }}</span>
        </div>
      </div>

      <div v-if="archive.length" class="manifest-archive">
        <strong>V012 留档</strong>
        <div v-for="item in archive" :key="item.id" class="manifest-meta">
          <span>{{ item.留档时间 }} · 批次 {{ item.导出批次 }} · {{ item.状态 }}</span>
          <span>{{ item.原因 }}</span>
          <span v-if="item.清理残留 && item.清理残留.length">清理残留：{{ item.清理残留.join('、') }}</span>
        </div>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记导出任务</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/data_export'
const columns = ["任务编号", "数据来源", "导出格式", "字段清单", "数据范围", "压缩方式", "分片大小", "导出批次"]
const strategies = ['按范围拆分', '合并文件']
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const scope = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])
const manifests = ref<any[]>([])
const archive = ref<any[]>([])
const manifestTask = ref('')
const manifestBatch = ref('')
const manifestStrategy = ref(strategies[0])
const manifestMessage = ref('')
const manifestOk = ref(true)
const manifestDetail = ref<any>(null)
const fieldNames = ref<string[]>([])
function scopeQuery() { return scope.value.trim() ? `?scope=${encodeURIComponent(scope.value.trim())}` : '' }
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try {
    const [tasksRes, manifestRes] = await Promise.all([
      request(`${ENDPOINT}${scopeQuery()}`),
      request(`${ENDPOINT}/manifests${scopeQuery()}`),
    ])
    const data = await tasksRes.json()
    allItems.value = data.items || []
    const manifestData = await manifestRes.json()
    manifests.value = manifestData.items || []
    if (!manifestTask.value && allItems.value.length) manifestTask.value = String(allItems.value[0].id)
    applyFilter()
    await loadArchive()
  } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export${scopeQuery()}`, '_blank') }
async function loadManifests() {
  const response = await request(`${ENDPOINT}/manifests${scopeQuery()}`)
  manifests.value = (await response.json()).items || []
  await loadArchive()
}
async function loadArchive() {
  const response = await request(`${ENDPOINT}/manifests/archive`)
  archive.value = ((await response.json()).items || []).slice(-5).reverse()
}
async function generateManifest() {
  const task = allItems.value.find((item: any) => String(item.id) === manifestTask.value)
  manifestMessage.value = ''; manifestDetail.value = null
  try {
    const response = await request(`${ENDPOINT}/manifests`, {
      method: 'POST',
      body: JSON.stringify({ values: {
        任务编号: task?.任务编号 || '',
        导出批次: manifestBatch.value.trim(),
        拆分策略: manifestStrategy.value,
        允许范围: scope.value.trim(),
      } }),
    })
    const payload = await response.json()
    manifestOk.value = Boolean(payload.ok)
    manifestMessage.value = payload.message
    if (payload.ok) await loadManifests()
    else await loadArchive()
  } catch (error) {
    manifestOk.value = false
    manifestMessage.value = error instanceof Error ? error.message : '清单生成失败'
  }
}
async function showManifest(batch: string) {
  const response = await request(`${ENDPOINT}/manifests/${encodeURIComponent(batch)}${scopeQuery()}`)
  manifestDetail.value = await response.json()
  fieldNames.value = (manifestDetail.value.字段清单 || []).map((field: any) => field.字段名)
}
async function verify(batch: string) {
  const response = await request(`${ENDPOINT}/manifests/${encodeURIComponent(batch)}/verify${scopeQuery()}`)
  const payload = await response.json()
  manifestOk.value = Boolean(payload.ok)
  manifestMessage.value = payload.ok
    ? `核对通过：${payload.核对文件数} 个文件、${payload.核对记录数} 条记录（${payload.核对项.join('、')}）`
    : `核对发现问题：${payload.问题.join('；')}`
  await loadArchive()
}
async function downloadFile(batch: string, fileName: string) {
  const response = await request(`${ENDPOINT}/manifests/${encodeURIComponent(batch)}/files/${encodeURIComponent(fileName)}/download${scopeQuery()}`)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: '下载失败' }))
    manifestOk.value = false
    manifestMessage.value = payload.detail || '下载失败'
    return
  }
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url; link.download = fileName; link.click(); URL.revokeObjectURL(url)
  manifestOk.value = true
  manifestMessage.value = `已下载 ${fileName}（字段清单、实验范围与指标口径随文件元数据一并给出）`
}
async function interrupt(batch: string, fileName: string) {
  const response = await request(`${ENDPOINT}/manifests/${encodeURIComponent(batch)}/files/${encodeURIComponent(fileName)}/interrupt`, {
    method: 'POST', body: JSON.stringify({ 分片: 1, remark: '页面模拟导出中断' }),
  })
  const payload = await response.json()
  manifestOk.value = Boolean(payload.ok); manifestMessage.value = payload.message
  await loadManifests()
}
async function resume(batch: string) {
  const response = await request(`${ENDPOINT}/manifests/${encodeURIComponent(batch)}/resume${scopeQuery()}`, { method: 'POST' })
  const payload = await response.json()
  manifestOk.value = Boolean(payload.ok); manifestMessage.value = payload.message
  await loadManifests()
}
onMounted(reload)
</script>
