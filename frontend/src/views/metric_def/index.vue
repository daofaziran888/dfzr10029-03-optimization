<template>
  <section class="page" data-module="metric_def">
    <header class="page-head">
      <div><h2>指标定义</h2><p class="page-desc">维护指标，围绕指标编号、指标名称、计算公式、统计粒度做登记、筛选与状态流转。</p></div>
    </header>
    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <section class="caliber-panel">
      <header class="caliber-head">
        <div>
          <h3>指标口径切换</h3>
          <p class="page-desc">按状态流转边界重算需刷新的指标，命中复用边界的已确认结果不再重复扫描。</p>
        </div>
        <span v-if="lastTask" class="caliber-task">最近任务：{{ lastTask['任务编号'] }}</span>
      </header>
      <form class="analysis-form" @submit.prevent="switchCaliber">
        <label><span>目标口径版本</span>
          <input v-model="switchForm['目标版本']" data-testid="caliber-version" placeholder="如 v2026.11" required />
        </label>
        <label><span>生效时间</span>
          <input v-model="switchForm['生效时间']" data-testid="caliber-effective" type="date" required />
        </label>
        <label><span>切换策略</span>
          <select v-model="switchForm['切换策略']" data-testid="caliber-strategy">
            <option v-for="option in strategies" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <button class="btn primary" type="submit" data-testid="caliber-switch" :disabled="switching">
          {{ switching ? '切换中…' : '发起口径切换' }}
        </button>
      </form>
      <div v-if="caliberStats.length" class="analysis-summary" data-testid="caliber-stats">
        <article v-for="item in caliberStats" :key="item.label" class="analysis-card">
          <span style="font-size:12px;color:var(--muted)">{{ item.label }}</span>
          <strong style="font-size:22px;display:block">{{ item.value }}</strong>
        </article>
      </div>
      <p v-if="caliberMessage" class="caliber-message" data-testid="caliber-message">{{ caliberMessage }}</p>
      <p v-if="caliberError" class="error-text" data-testid="caliber-error">{{ caliberError }}</p>

      <table v-if="refreshRows.length" class="data-table" data-testid="caliber-refresh-list">
        <thead>
          <tr>
            <th>指标编号</th><th>生命周期状态</th><th>刷新方式</th><th>刷新状态</th>
            <th>指标值</th><th>状态水位</th><th>核对结果</th><th>记录版本</th><th>核对</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in refreshRows" :key="String(row.id)" :data-testid="`caliber-row-${row['指标编号']}`">
            <td>{{ row['指标编号'] }}</td>
            <td>{{ row['生命周期状态'] }}</td>
            <td>{{ row['刷新方式'] }}</td>
            <td>{{ row['刷新状态'] }}</td>
            <td>{{ row['指标值'] ?? '—' }}</td>
            <td>{{ row['本次水位'] ?? '—' }}</td>
            <td>{{ row['核对结果'] ?? '—' }}</td>
            <td>{{ row['记录版本'] }}</td>
            <td>
              <button class="link" type="button" data-testid="caliber-check" @click="writeDiff(row)">写入核对结果</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!refreshRows.length && !caliberLoading" style="color:var(--muted);font-size:13px">
        暂无刷新清单，发起口径切换后按状态流转边界生成。
      </p>
    </section>

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
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/metric_def'
const strategies = ['按发布日期水位继续', '按业务窗口全量重放']
const summaryMetrics = ref<{label:string;value:number;trend:number}[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本"].slice(0, 5))

type SwitchForm = { 目标版本: string; 生效时间: string; 切换策略: string }
const switchForm = ref<SwitchForm>({ 目标版本: '', 生效时间: '', 切换策略: strategies[0] })
const switching = ref(false)
const caliberLoading = ref(false)
const caliberMessage = ref('')
const caliberError = ref('')
const lastTask = ref<Record<string, any> | null>(null)
const refreshRows = ref<Record<string, any>[]>([])
const caliberStats = computed(() => {
  const task = lastTask.value
  if (!task) return []
  return [
    { label: '本次重算指标数', value: task['重算指标数'] ?? 0 },
    { label: '本次复用指标数', value: task['复用指标数'] ?? 0 },
    { label: '刷新耗时(ms)', value: task['刷新耗时毫秒'] ?? 0 },
    { label: '状态流转边界', value: task['状态边界数'] ?? 0 },
  ]
})

function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
async function fetchSummary() { try { const res = await request(`${ENDPOINT}/summary`); summaryMetrics.value = (await res.json()).metrics || [] } catch(e) {} }
async function fetchLastTask() {
  caliberLoading.value = true
  try {
    const res = await request(`${ENDPOINT}/caliber/tasks`)
    const tasks = (await res.json()).items || []
    if (tasks.length) {
      lastTask.value = tasks[tasks.length - 1]
      refreshRows.value = (await (await request(`${ENDPOINT}/caliber/refresh-list?task_no=${encodeURIComponent(lastTask.value!['任务编号'])}`)).json()).items || []
    }
  } catch (e) {
    caliberError.value = e instanceof Error ? e.message : '口径切换任务读取失败'
  } finally {
    caliberLoading.value = false
  }
}
async function switchCaliber() {
  switching.value = true
  caliberError.value = ''
  caliberMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/caliber/switch`, {
      method: 'POST', body: JSON.stringify({ values: switchForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '口径切换失败')
    lastTask.value = payload.task
    refreshRows.value = payload.items || []
    caliberMessage.value = payload.message
    results.value = payload.task_results || []
    chartData.value = results.value.slice(0, 6).map((row: any) => ({
      label: String(row['指标编号'] ?? ''), value: Number(row['指标值'] ?? 0),
    }))
  } catch (error) {
    caliberError.value = error instanceof Error ? error.message : '口径切换失败'
  } finally {
    switching.value = false
  }
}
async function writeDiff(row: Record<string, any>) {
  caliberError.value = ''
  try {
    const response = await request(`${ENDPOINT}/caliber/diff`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          任务编号: lastTask.value?.['任务编号'],
          items: [{ 指标编号: row['指标编号'], 生命周期状态: row['生命周期状态'], 指标值: row['指标值'] }],
        },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '核对结果写入失败')
    caliberMessage.value = payload.message
    const refreshed = await request(`${ENDPOINT}/caliber/refresh-list?task_no=${encodeURIComponent(lastTask.value?.['任务编号'] ?? '')}`)
    refreshRows.value = (await refreshed.json()).items || []
  } catch (error) {
    caliberError.value = error instanceof Error ? error.message : '核对结果写入失败'
  }
}
async function runAnalysis() {
  loading.value = true
  try { const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) }); const data = await res.json(); results.value = data.items || []; chartData.value = (data.items || []).map((r: any) => ({ label: Object.values(r)[0] as string, value: Object.values(r).filter(v => typeof v === 'number')[0] as number || 0 })).slice(0, 6) } catch(e) { results.value = [] }
  loading.value = false
}
onMounted(() => { fetchSummary(); fetchLastTask() })
</script>
