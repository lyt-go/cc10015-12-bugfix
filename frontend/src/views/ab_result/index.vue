<template>
  <section class="page" data-module="ab_result">
    <header class="page-head">
      <div><h2>AB结果分析</h2><p class="page-desc">看板、列表与详情共用同一套稳定结果，并按允许的实验范围裁剪。</p></div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出结果清单</button>
      </div>
    </header>
    <div class="scope-bar">
      <label><span>允许查看的实验范围</span>
        <input v-model="scope" data-testid="scope-input" placeholder="如 EXPE-0001，留空表示不裁剪" />
      </label>
      <button class="btn" type="button" data-testid="scope-apply" @click="reload">按范围重新查看</button>
      <span class="scope-hint">范围外记录不会出现在看板、列表与详情中</span>
    </div>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th><th>指标口径</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="row in results" :key="row.id" :data-testid="`result-${row.id}`">
          <td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td>
          <td>{{ caliberText(row) }}</td>
          <td><button class="link" type="button" @click="showDetail(row.id)">详情</button></td>
        </tr>
      </tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">当前范围内暂无可查看的分析结果</p>
    <div v-if="detail" class="manifest-detail" data-testid="result-detail">
      <header class="manifest-card-head"><strong>结果详情 {{ detail.报告编号 }}</strong><button class="link" type="button" @click="detail = null">收起</button></header>
      <div class="manifest-meta">
        <span>实验归属：{{ detail.实验编号 }}</span>
        <span>指标键：{{ detail.指标编号 }}</span>
        <span>指标值：{{ detail.指标值 }}</span>
        <span>对比基准：{{ detail.对照组均值 }}</span>
        <span>差异：{{ detail.差异 }}</span>
        <span>口径：{{ caliberText(detail) }}</span>
      </div>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/ab_result'
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const scope = ref('')
const detail = ref<any>(null)
const params = ref<Record<string,string>>({})
const paramFields = ["报告编号", "实验编号", "分析指标", "对照组均值", "实验组均值", "置信水平", "效果方向", "生成时间"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["报告编号", "实验编号", "指标编号", "对照组均值", "实验组均值", "差异"])
function scopeQuery() { return scope.value.trim() ? `?scope=${encodeURIComponent(scope.value.trim())}` : '' }
function caliberText(row: any) { const c = row?.指标口径; return c ? `${c.指标键}@${c.精度位数}` : '—' }
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary${scopeQuery()}`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function reload() {
  detail.value = null
  loading.value = true
  try {
    const res = await request(`${ENDPOINT}${scopeQuery()}`)
    results.value = (await res.json()).items || []
    chartData.value = results.value.map((r: any) => ({ label: r.报告编号, value: Number(r.指标值) || 0 })).slice(0, 6)
    await fetchSummary()
  } catch(e) { results.value = [] }
  loading.value = false
}
async function showDetail(id: number) {
  const response = await request(`${ENDPOINT}/${id}`)
  detail.value = response.ok ? await response.json() : null
}
function exportRows() { window.open(`${ENDPOINT}/export${scopeQuery()}`, '_blank') }
async function runAnalysis() {
  loading.value = true
  try {
    const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
    const data = await res.json()
    results.value = data.items || []
    chartData.value = (data.items || []).map((r: any) => ({ label: String(r.分组), value: Number(r.指标值) || 0 })).slice(0, 6)
  } catch(e) { results.value = [] }
  loading.value = false
}
onMounted(reload)
</script>
