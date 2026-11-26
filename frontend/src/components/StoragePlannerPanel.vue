<template>
  <section class="planner-panel" data-testid="storage-planner">
    <header class="planner-head">
      <h3>冷热容量试算 · 跨层导出</h3>
      <p class="planner-tip">
        同一套策略同时决定容量、分层日期区间与导出预算：保留期内热层与冷层日期不重叠；
        热层天数超过总保留时只占实际留存天数；热层字节 + 冷层字节 = 总量。
      </p>
    </header>

    <form class="planner-form" data-testid="planner-form" @submit.prevent="recalculate">
      <label class="planner-field">
        <span>总保留天数</span>
        <input
          v-model.number="form.retention_days"
          type="number" min="0"
          data-testid="retention-days"
          aria-label="总保留天数"
        />
      </label>
      <label class="planner-field">
        <span>热层天数</span>
        <input
          v-model.number="form.hot_days"
          type="number" min="0"
          data-testid="hot-days"
          aria-label="热层天数"
        />
      </label>
      <label class="planner-field">
        <span>每日点数</span>
        <input
          v-model.number="form.points_per_day"
          type="number" min="0"
          data-testid="points-per-day"
          aria-label="每日点数"
        />
      </label>
      <label class="planner-field">
        <span>每点字节</span>
        <input
          v-model.number="form.bytes_per_point"
          type="number" min="1"
          data-testid="bytes-per-point"
          aria-label="每点字节"
        />
      </label>
      <label class="planner-field">
        <span>压缩方式</span>
        <select v-model="form.compression" data-testid="compression" aria-label="压缩方式">
          <option value="none">无压缩</option>
          <option value="gzip">gzip（5:1）</option>
          <option value="snappy">snappy（2:1）</option>
          <option value="lz4">lz4（3:1）</option>
          <option value="zstd">zstd（6:1）</option>
        </select>
      </label>
      <label class="planner-field">
        <span>准备导出天数（可选）</span>
        <input
          v-model.number="form.requested_days"
          type="number" min="0"
          placeholder="例如 10"
          data-testid="export-days"
          aria-label="准备导出天数"
        />
      </label>
      <label v-if="showStrategyRef" class="planner-field">
        <span>策略编号（可选）</span>
        <input v-model="form.strategy_ref" data-testid="strategy-ref" aria-label="策略编号" />
      </label>

      <div class="planner-actions">
        <button class="btn" type="submit" data-testid="calculate-capacity">容量试算</button>
        <button
          class="btn primary"
          type="button"
          data-testid="submit-cross-tier-export"
          :disabled="submitting"
          @click="submitExport"
        >
          {{ submitting ? '提交中…' : '提交跨层导出' }}
        </button>
        <button
          class="btn ghost"
          type="button"
          data-testid="update-retention"
          :disabled="!lastPlanNo"
          @click="updateRetention"
        >
          更新保留并清理冷层
        </button>
      </div>
    </form>

    <p v-if="boundStrategy" class="bound-tip" data-testid="bound-strategy">
      已关联冷热策略表行：<strong>{{ boundStrategy }}</strong>
    </p>
    <p v-if="errorMessage" class="error-text" data-testid="planner-error">{{ errorMessage }}</p>
    <p v-if="noticeMessage" class="notice-text" data-testid="planner-notice">{{ noticeMessage }}</p>

    <div v-if="capacity" class="capacity-result" data-testid="capacity-result">
      <article class="cap-card hot">
        <span class="cap-label">热层容量（字节）</span>
        <strong class="cap-value" data-testid="hot-bytes"><span data-testid="hot-storage-bytes">{{ capacity.hot_bytes }}</span></strong>
        <span class="cap-days" data-testid="hot-days-actual">实际热层 {{ capacity.hot_days }} 天</span>
      </article>
      <article class="cap-card cold">
        <span class="cap-label">冷层容量（字节）</span>
        <strong class="cap-value" data-testid="cold-bytes"><span data-testid="cold-storage-bytes">{{ capacity.cold_bytes }}</span></strong>
        <span class="cap-days" data-testid="cold-days-actual">实际冷层 {{ capacity.cold_days }} 天</span>
      </article>
      <article class="cap-card total">
        <span class="cap-label">总保留容量（字节）</span>
        <strong class="cap-value" data-testid="total-bytes"><span data-testid="total-storage-bytes">{{ capacity.total_bytes }}</span></strong>
        <span class="cap-days">热层 + 冷层 = {{ Number(capacity.hot_bytes) + Number(capacity.cold_bytes) }}</span>
      </article>
      <p class="capacity-line" data-testid="capacity-line">
        按每天 {{ capacity.points_per_day }} 点、每点 {{ capacity.bytes_per_point }} 字节、
        {{ capacity.compression === 'none' ? '无压缩' : capacity.compression }} 计算：
        热层 <strong>{{ capacity.hot_bytes }}</strong> 字节，
        冷层 <strong>{{ capacity.cold_bytes }}</strong> 字节，
        总量 <strong>{{ capacity.total_bytes }}</strong> 字节。
      </p>
    </div>

    <div v-if="plan" class="read-plan" data-testid="read-plan">
      <div class="plan-head">
        <h4>分层读取计划（实际读取区间可核对）</h4>
        <RouterLink
          class="btn primary"
          :to="`/batch_export/${plan.task_id ?? plan.task_no ?? plan.plan_no}`"
          data-testid="link-export-detail"
        >
          查看导出详情（{{ plan.task_no || plan.plan_no }}）
        </RouterLink>
      </div>

      <div v-if="plan.has_conflict && plan.conflict" class="conflict-banner" data-testid="conflict-banner">
        ⚠️ {{ plan.conflict.message }}
      </div>

      <div class="tier-summary">
        <div v-for="tier in plan.tiers" :key="tier.tier" class="tier-box" :data-testid="`tier-${tier.tier}`">
          <strong>{{ tier.name }}：{{ tier.days }} 天</strong>
          <span class="tier-interval">{{ tier.read_interval || '无该层日期' }}</span>
          <span class="tier-bytes">{{ tier.bytes }} 字节</span>
        </div>
        <div class="tier-box unavailable">
          <strong>超出保留期：{{ plan.unavailable_days }} 天</strong>
          <span class="tier-interval">提交导出时不安排读取、不计费</span>
        </div>
      </div>

      <table class="data-table plan-table">
        <thead>
          <tr>
            <th>日期</th>
            <th>相对日</th>
            <th>层级</th>
            <th>实际读取区间</th>
            <th>字节</th>
            <th>可导出</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="day in plan.dates"
            :key="day.date + day.offset"
            :data-testid="`plan-row-${day.date}`"
            :data-tier="day.tier"
          >
            <td>{{ day.date }} {{ day.weekday }}</td>
            <td>{{ relativeLabel(day.offset) }}</td>
            <td>{{ day.tier_name }}</td>
            <td class="interval-cell" data-testid="read-interval">{{ day.read_interval }}</td>
            <td>{{ day.bytes }}</td>
            <td>{{ day.available ? '是' : '否（超出保留）' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'

import {
  changeRetention,
  createCrossTierExport,
  fetchCapacity,
  type ReadPlan,
} from '@/api/tiered'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ strategy?: EntryRow | null }>()

const form = reactive({
  retention_days: 5 as number | string,
  hot_days: 30 as number | string,
  points_per_day: 100 as number | string,
  bytes_per_point: 32 as number | string,
  compression: 'none',
  requested_days: '' as number | string,
  strategy_ref: '' as string | number,
})

const showStrategyRef = ref(false)
const boundStrategy = ref('')
const capacity = ref<ReadPlan | null>(null)
const plan = ref<ReadPlan | null>(null)
const lastPlanNo = ref('')
const submitting = ref(false)
const errorMessage = ref('')
const noticeMessage = ref('')

function toInt(value: number | string, fallback: number): number {
  if (value === '' || value === null || value === undefined) return fallback
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function parseDaysLike(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  const match = /(\d+)/.exec(String(value ?? ''))
  return match ? Number(match[1]) : fallback
}

function prefillFromStrategy(row: EntryRow) {
  form.strategy_ref = String(row['策略编号'] ?? row.id)
  form.retention_days = parseDaysLike(row['保留天数'], 5)
  form.hot_days = parseDaysLike(row['热层天数'] ?? row['冷热分层'], 30)
  const compression = String(row['压缩算法'] ?? 'none')
  form.compression = ['none', 'gzip', 'snappy', 'lz4', 'zstd'].includes(compression)
    ? compression
    : /无|不压缩|none/i.test(compression) ? 'none' : 'none'
  const ppp = parseDaysLike(row['每日点数'], 100)
  form.points_per_day = ppp
  form.bytes_per_point = parseDaysLike(row['每点字节'], 32)
  boundStrategy.value = `${String(row['策略编号'] ?? row.id)}（测点 ${String(row['测点编号'] ?? '-')}）`
  showStrategyRef.value = true
}

watch(
  () => props.strategy,
  (row) => {
    if (row) {
      prefillFromStrategy(row)
      void recalculate()
    }
  },
  { immediate: true },
)

async function recalculate() {
  errorMessage.value = ''
  try {
    const params: Record<string, number | string> = {
      retention_days: toInt(form.retention_days, 5),
      hot_days: toInt(form.hot_days, 30),
      points_per_day: toInt(form.points_per_day, 100),
      bytes_per_point: toInt(form.bytes_per_point, 32),
      compression: form.compression,
    }
    if (form.requested_days !== '') params.requested_days = toInt(form.requested_days, 10)
    capacity.value = await fetchCapacity(params)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '容量试算失败'
  }
}

async function submitExport() {
  submitting.value = true
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const body: Record<string, unknown> = {
      retention_days: toInt(form.retention_days, 5),
      hot_days: toInt(form.hot_days, 30),
      points_per_day: toInt(form.points_per_day, 100),
      bytes_per_point: toInt(form.bytes_per_point, 32),
      compression: form.compression,
    }
    if (form.requested_days !== '') body.requested_days = toInt(form.requested_days, 10)
    if (form.strategy_ref !== '') body.strategy_ref = form.strategy_ref
    const result = await createCrossTierExport(body)
    if (!result.ok) throw new Error(result.message || '提交失败')
    plan.value = result.plan
    lastPlanNo.value = result.plan_no
    noticeMessage.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '跨层导出提交失败'
  } finally {
    submitting.value = false
  }
}

async function updateRetention() {
  if (!lastPlanNo.value) return
  errorMessage.value = ''
  try {
    const result = await changeRetention(lastPlanNo.value, {
      retention_days: toInt(form.retention_days, 5),
      hot_days: toInt(form.hot_days, 30),
      strategy_ref: form.strategy_ref || undefined,
    })
    if (!result.ok) throw new Error(result.message || '保留策略更新失败')
    if (result.plan) plan.value = result.plan
    noticeMessage.value = result.conflict?.message || result.message
    await recalculate()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保留策略更新失败'
  }
}

function relativeLabel(offset: number): string {
  if (offset === 0) return '今天（D0）'
  if (offset === 1) return '昨天（D-1）'
  return `D-${offset}`
}

/** 冷热策略表行内“跨层导出”入口：回填策略 → 试算 → 直接提交计划。 */
async function bindAndSubmit(row: EntryRow) {
  prefillFromStrategy(row)
  await recalculate()
  await submitExport()
}

defineExpose({ bindAndSubmit, recalculate })

onMounted(recalculate)
</script>

<style scoped>
.planner-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.planner-head h3 { margin: 0 0 4px; font-size: 16px; }
.planner-tip { margin: 0 0 10px; color: var(--muted); font-size: 12.5px; line-height: 1.6; }
.planner-form { display: grid; grid-template-columns: repeat(3, minmax(150px, 1fr)); gap: 10px 12px; align-items: end; }
.planner-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.planner-field input, .planner-field select {
  border: 1px solid var(--border); border-radius: 6px; padding: 7px 8px; font-size: 13px; color: #1f2937;
}
.planner-actions { grid-column: 1 / -1; display: flex; gap: 8px; flex-wrap: wrap; }
.bound-tip { font-size: 12.5px; color: #1d4ed8; margin: 8px 0 0; }
.notice-text { color: #15803d; font-size: 13px; margin: 8px 0 0; }
.error-text { color: #b42318; font-size: 13px; margin: 8px 0 0; }
.capacity-result { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 12px; }
.cap-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; }
.cap-card.hot { border-color: #f59e0b; background: #fffbeb; }
.cap-card.cold { border-color: #3b82f6; background: #eff6ff; }
.cap-card.total { border-color: #22c55e; background: #f0fdf4; }
.cap-label { font-size: 12px; color: var(--muted); }
.cap-value { font-size: 22px; }
.cap-days { font-size: 12px; color: var(--muted); }
.capacity-line { grid-column: 1 / -1; font-size: 13px; margin: 2px 0 0; }
.read-plan { margin-top: 14px; border-top: 1px dashed var(--border); padding-top: 12px; }
.plan-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.plan-head h4 { margin: 0; font-size: 15px; }
.conflict-banner {
  background: #fef3c7; border: 1px solid #f59e0b; color: #92400e;
  border-radius: 8px; padding: 8px 10px; font-size: 13px; margin: 10px 0;
}
.tier-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 10px 0; }
.tier-box { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; font-size: 12.5px; }
.tier-box.unavailable { background: #f8fafc; color: var(--muted); }
.tier-interval { color: var(--muted); word-break: break-all; }
.tier-bytes { font-weight: 600; }
.plan-table .interval-cell { font-size: 12px; white-space: nowrap; }
</style>
