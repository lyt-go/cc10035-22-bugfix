<template>
  <section class="export-detail page" data-testid="export-detail">
    <header class="page-head">
      <div>
        <h2>导出详情 · 分层读取计划核对</h2>
        <p class="page-desc">
          任务 {{ ref }}：核对热层日期、实际收到的冷段与每段状态；费用只按实际可取且确认交付的范围计算。
        </p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" to="/batch_export" data-testid="back-to-list">返回批量导出列表</RouterLink>
        <button class="btn" type="button" data-testid="refresh-detail" @click="reload">刷新</button>
      </div>
    </header>

    <p v-if="loading" class="muted-text">加载中…</p>
    <p v-if="errorMessage" class="error-text" data-testid="detail-error">{{ errorMessage }}</p>

    <template v-if="detail">
      <!-- 任务概要 -->
      <div class="detail-grid">
        <article class="info-card" data-testid="task-summary">
          <h3>任务信息</h3>
          <dl>
            <dt>任务编号</dt><dd data-testid="task-no">{{ taskNo }}</dd>
            <dt>测点列表</dt><dd>{{ detail.task?.['测点列表'] ?? '—' }}</dd>
            <dt>时间范围</dt><dd>{{ detail.task?.['时间范围'] ?? '—' }}</dd>
            <dt>导出格式</dt><dd>{{ detail.task?.['导出格式'] ?? '—' }}</dd>
            <dt>任务状态</dt><dd data-testid="task-status">{{ detail.task?.['status'] ?? '—' }}</dd>
          </dl>
        </article>

        <!-- 策略调整（与冷热策略表同一套参数） -->
        <article class="info-card" data-testid="retention-editor">
          <h3>保留策略（与冷热策略表同一套）</h3>
          <div class="mini-form">
            <label><span>总保留天数</span>
              <input v-model.number="retentionForm.retention_days" type="number" min="0"
                     data-testid="detail-retention-days" aria-label="详情页总保留天数" />
            </label>
            <label><span>热层天数</span>
              <input v-model.number="retentionForm.hot_days" type="number" min="0"
                     data-testid="detail-hot-days" aria-label="详情页热层天数" />
            </label>
            <button class="btn primary" type="button"
                    data-testid="detail-update-retention" @click="applyRetention">
              应用并清理冷层
            </button>
          </div>
          <p class="muted-text small">保留收缩会与在途读取计划对账：等待读取 / 不可读 / 已交付。</p>
        </article>
      </div>

      <!-- 尚无计划：允许在详情页直接生成 -->
      <article v-if="!plan" class="info-card" data-testid="plan-empty">
        <h3>该任务还没有分层读取计划</h3>
        <div class="mini-form">
          <label><span>总保留天数</span><input v-model.number="prepareForm.retention_days" type="number" min="0" data-testid="prepare-retention" /></label>
          <label><span>热层天数</span><input v-model.number="prepareForm.hot_days" type="number" min="0" data-testid="prepare-hot" /></label>
          <label><span>导出天数</span><input v-model.number="prepareForm.requested_days" type="number" min="0" data-testid="prepare-days" /></label>
          <label><span>每日点数</span><input v-model.number="prepareForm.points_per_day" type="number" min="0" data-testid="prepare-points" /></label>
          <button class="btn primary" type="button" data-testid="prepare-plan" @click="preparePlan">生成分层读取计划</button>
        </div>
      </article>

      <template v-if="plan">
        <div v-if="plan.has_conflict && plan.conflict" class="conflict-banner" data-testid="conflict-banner">
          ⚠️ <span data-testid="conflict-message">{{ plan.conflict.message }}</span>
        </div>

        <!-- 分层与预算 -->
        <div class="metric-row">
          <article class="metric-card hot">
            <span>热层天数</span>
            <strong data-testid="hot-days-count">{{ plan.hot_days }}</strong>
            <em>{{ plan.hot_bytes }} 字节</em>
          </article>
          <article class="metric-card cold">
            <span>冷层天数（当前策略）</span>
            <strong data-testid="cold-days-count">{{ plan.cold_days }}</strong>
            <em>{{ plan.cold_bytes }} 字节</em>
          </article>
          <article class="metric-card">
            <span>请求导出 / 可安排</span>
            <strong><span data-testid="requested-days">{{ plan.requested_days }}</span> /
              <span data-testid="schedulable-days">{{ plan.schedulable_days }}</span></strong>
            <em>{{ plan.unavailable_days }} 天超出保留期</em>
          </article>
          <article class="metric-card bill">
            <span>当前应计费天数</span>
            <strong data-testid="billable-days">{{ billing?.billable_days ?? 0 }}</strong>
            <em><span data-testid="billable-bytes">{{ billing?.billable_bytes ?? 0 }}</span> 字节</em>
          </article>
          <article class="metric-card">
            <span>实际仍可取天数</span>
            <strong data-testid="retrievable-days">{{ billing?.retrievable_days ?? 0 }}</strong>
            <em><span data-testid="retrievable-bytes">{{ billing?.retrievable_bytes ?? 0 }}</span> 字节</em>
          </article>
        </div>

        <p class="fee-basis" data-testid="fee-basis">
          费用依据实际可取的范围：热层 {{ billing?.hot_days ?? plan.hot_days }} 天可直接读取，
          冷段以读取确认（已交付）为准；确认前失去有效读取依据的日期不收费，
          已发出的片段不会重发。当前计费
          <strong>{{ billing?.billable_days ?? 0 }}</strong> 天 /
          <strong>{{ billing?.billable_points ?? 0 }}</strong> 点 /
          <strong>{{ billing?.billable_bytes ?? 0 }}</strong> 字节。
        </p>

        <div class="detail-grid">
          <!-- 热层日期：固定 3 天等场景可核对 -->
          <article class="info-card">
            <h3>热层读取区间（{{ hotDays.length }} 天）</h3>
            <table class="data-table">
              <thead><tr><th>日期</th><th>实际读取区间</th><th>字节</th><th>状态</th></tr></thead>
              <tbody>
                <tr v-for="day in hotDays" :key="`hot-${day.date}`" :data-testid="`hot-row-${day.date}`">
                  <td>{{ day.date }}</td>
                  <td class="interval-cell">{{ day.read_interval }}</td>
                  <td>{{ day.bytes }}</td>
                  <td>热层可直接读取</td>
                </tr>
                <tr v-if="!hotDays.length"><td colspan="4" class="empty-state">当前策略下没有热层日期</td></tr>
              </tbody>
            </table>
          </article>

          <!-- 超期日期 -->
          <article class="info-card">
            <h3>超出保留期（不可安排、不计费）</h3>
            <table class="data-table">
              <thead><tr><th>日期</th><th>请求区间</th></tr></thead>
              <tbody>
                <tr v-for="day in unavailableDays" :key="`na-${day.date}`" :data-testid="`unavailable-row-${day.date}`">
                  <td>{{ day.date }}</td>
                  <td class="interval-cell">{{ day.read_interval }}</td>
                </tr>
                <tr v-if="!unavailableDays.length"><td colspan="2" class="empty-state">请求范围均在保留期内</td></tr>
              </tbody>
            </table>
          </article>
        </div>

        <!-- 冷段逐段核对 -->
        <article class="info-card segments-card">
          <div class="segments-head">
            <h3>冷层分段：逐段核对（实际收到 {{ deliveredCount }} 段）</h3>
            <button class="btn" type="button" data-testid="claim-older-three" @click="claimOlderThree">
              领取最早的 3 个冷日
            </button>
          </div>
          <table class="data-table segments-table" data-testid="segments-table">
            <thead>
              <tr>
                <th>冷段日期</th><th>实际读取区间</th><th>大小</th>
                <th>服务端状态</th><th>片段</th><th>逐段操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="seg in plan.segments"
                :key="seg.segment_id"
                :data-testid="`segment-row-${seg.segment_id}`"
                :data-segment-status="seg.state_code || seg.status"
                :data-date="seg.date"
                :class="{ 'row-unreadable': isUnreadable(seg), 'row-delivered': isDelivered(seg) }"
              >
                <td>
                  <span :data-testid="`segment-${seg.date}`">{{ seg.date }}</span>
                </td>
                <td class="interval-cell">{{ seg.read_interval }}</td>
                <td>{{ seg.bytes }} 字节</td>
                <td>
                  <span class="status-tag" :class="`tag-${seg.state_code || ''}`"
                        :data-testid="`segment-status-${seg.date}`">
                    {{ seg.status }}
                  </span>
                  <span v-if="isUnreadable(seg)" class="released-badge"
                        :data-testid="`segment-released-${seg.date}`">
                    {{ seg.display_status || '已被释放' }}
                  </span>
                </td>
                <td class="small">
                  已发 {{ seg.sent_fragments }}/{{ seg.fragments.length }}，
                  交付 {{ seg.delivered_fragments }}/{{ seg.fragments.length }}
                  <div v-if="isDelivered(seg)" class="muted-text">片段已发出，不会重发</div>
                </td>
                <td class="segment-actions">
                  <template v-if="isPending(seg)">
                    <button class="btn" type="button"
                            :data-testid="`claim-${seg.segment_id}`"
                            @click="runSegment(seg.segment_id, 'claim')">领取冷段</button>
                    <button class="btn primary" type="button"
                            :data-testid="`continue-${seg.segment_id}`"
                            @click="runSegment(seg.segment_id, 'complete')">继续完成</button>
                  </template>
                  <template v-else-if="isWaiting(seg)">
                    <span class="waiting-text" :data-testid="`waiting-${seg.segment_id}`">等待读取</span>
                    <button class="btn" type="button"
                            :data-testid="`read-${seg.segment_id}`"
                            @click="runSegment(seg.segment_id, 'read')">读取片段</button>
                    <button class="btn" type="button"
                            :data-testid="`confirm-${seg.segment_id}`"
                            @click="runSegment(seg.segment_id, 'confirm')">确认读段</button>
                    <button class="btn primary" type="button"
                            :data-testid="`continue-${seg.segment_id}`"
                            @click="runSegment(seg.segment_id, 'complete')">继续完成</button>
                  </template>
                  <span v-else-if="isDelivered(seg)" class="delivered-text"
                        :data-testid="`delivered-${seg.segment_id}`">✓ 已交付（已计费，不重发）</span>
                  <span v-else class="released-text"
                        :data-testid="`released-${seg.segment_id}`">已被释放，不可继续</span>
                </td>
              </tr>
              <tr v-if="!plan.segments.length">
                <td colspan="6" class="empty-state">当前策略下没有冷层日期（热层已覆盖全部保留天数）</td>
              </tr>
            </tbody>
          </table>
        </article>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import {
  claimSegments,
  fetchExportDetail,
  segmentAction,
  changeRetention,
  type Billing,
  type ReadPlan,
  type Segment,
  type TierDay,
} from '@/api/tiered'

const route = useRoute()
const refId = computed(() => String(route.params.ref ?? route.params.taskRef ?? ''))

const detail = ref<Awaited<ReturnType<typeof fetchExportDetail>> | null>(null)
const loading = ref(false)
const errorMessage = ref('')
const actionMessage = ref('')

const retentionForm = reactive({ retention_days: 5 as number | string, hot_days: 3 as number | string })
const prepareForm = reactive({
  retention_days: 8 as number | string,
  hot_days: 3 as number | string,
  requested_days: 10 as number | string,
  points_per_day: 100 as number | string,
})

const plan = computed<ReadPlan | null>(() => detail.value?.plan ?? null)
const billing = computed<Billing | null>(() => detail.value?.billing ?? plan.value?.billing ?? null)
const taskNo = computed(() => String(detail.value?.task?.['任务编号'] ?? refId.value))
const hotDays = computed<TierDay[]>(() => plan.value?.dates.filter((d) => d.tier === 'hot') ?? [])
const unavailableDays = computed<TierDay[]>(() => plan.value?.dates.filter((d) => d.tier === 'unavailable') ?? [])
const deliveredCount = computed(() => plan.value?.segments.filter((s) => isDelivered(s)).length ?? 0)

function code(seg: Segment): string {
  return String(seg.state_code || seg.state || seg.status)
}
function isPending(seg: Segment): boolean {
  return code(seg) === 'pending' || seg.status === '待领取'
}
function isWaiting(seg: Segment): boolean {
  return code(seg) === 'waiting_read' || seg.status === '等待读取'
}
function isDelivered(seg: Segment): boolean {
  return code(seg) === 'delivered' || seg.status === '已交付'
}
function isUnreadable(seg: Segment): boolean {
  return code(seg) === 'unreadable' || seg.status === '不可读' || seg.released
}

async function reload() {
  loading.value = true
  errorMessage.value = ''
  try {
    detail.value = await fetchExportDetail(refId.value)
    if (detail.value.plan) {
      retentionForm.retention_days = detail.value.plan.retention_days
      retentionForm.hot_days = detail.value.plan.hot_days_configured
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出详情读取失败'
  } finally {
    loading.value = false
  }
}

async function preparePlan() {
  errorMessage.value = ''
  try {
    const response = await fetch(`/api/batch_export/${encodeURIComponent(refId.value)}/read-plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...prepareForm, bytes_per_point: 32, compression: 'none' }),
    })
    const result = await response.json()
    if (!result.ok) throw new Error(result.message || '计划生成失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '计划生成失败'
  }
}

async function runSegment(segmentId: string, action: 'claim' | 'read' | 'confirm' | 'complete') {
  errorMessage.value = ''
  try {
    const result = await segmentAction(plan.value?.plan_no ?? refId.value, segmentId, action)
    if (!result.ok) {
      errorMessage.value = result.message
    } else {
      actionMessage.value = result.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分段操作失败'
  }
}

async function claimOlderThree() {
  errorMessage.value = ''
  try {
    const result = await claimSegments(plan.value?.plan_no ?? refId.value, { count: 3, pick: 'older' })
    if (!result.ok) errorMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量领取失败'
  }
}

async function applyRetention() {
  errorMessage.value = ''
  try {
    const result = await changeRetention(plan.value?.plan_no ?? refId.value, {
      retention_days: Number(retentionForm.retention_days),
      hot_days: Number(retentionForm.hot_days),
    })
    if (!result.ok) throw new Error(result.message || '保留策略更新失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '保留策略更新失败'
  }
}

watch(() => route.params.ref, reload)
onMounted(reload)
</script>

<style scoped>
.muted-text { color: var(--muted); font-size: 12.5px; }
.small { font-size: 12px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0; }
.info-card { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.info-card h3 { margin: 0 0 10px; font-size: 15px; }
.info-card dl { display: grid; grid-template-columns: 80px 1fr; gap: 6px 10px; margin: 0; font-size: 13px; }
.info-card dt { color: var(--muted); }
.info-card dd { margin: 0; }
.mini-form { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
.mini-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.mini-form input { width: 120px; border: 1px solid var(--border); border-radius: 6px; padding: 7px 8px; }
.conflict-banner { background: #fef3c7; border: 1px solid #f59e0b; color: #92400e; border-radius: 8px; padding: 10px 12px; font-size: 13.5px; margin: 10px 0; }
.metric-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 12px 0; }
.metric-card { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: var(--muted); }
.metric-card strong { font-size: 22px; color: #1f2937; }
.metric-card em { font-style: normal; font-size: 12px; }
.metric-card.hot { border-color: #f59e0b; background: #fffbeb; }
.metric-card.cold { border-color: #3b82f6; background: #eff6ff; }
.metric-card.bill { border-color: #22c55e; background: #f0fdf4; }
.fee-basis { background: #f8fafc; border-left: 3px solid var(--brand); padding: 8px 12px; font-size: 13px; border-radius: 0 8px 8px 0; }
.interval-cell { font-size: 12px; white-space: nowrap; }
.segments-card { margin-top: 12px; }
.segments-head { display: flex; justify-content: space-between; align-items: center; }
.segments-head h3 { margin: 0; }
.segment-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.status-tag { padding: 2px 8px; border-radius: 999px; font-size: 12px; background: #e5e7eb; }
.tag-waiting_read { background: #fef3c7; color: #92400e; }
.tag-delivered { background: #dcfce7; color: #166534; }
.tag-unreadable { background: #fee2e2; color: #991b1b; }
.released-badge { margin-left: 6px; padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #fee2e2; color: #991b1b; border: 1px solid #fca5a5; }
.waiting-text { color: #92400e; font-size: 12.5px; }
.delivered-text { color: #166534; font-size: 12.5px; }
.released-text { color: #991b1b; font-size: 12.5px; }
.row-unreadable { background: #fef2f2; }
.row-delivered { background: #f0fdf4; }
</style>
