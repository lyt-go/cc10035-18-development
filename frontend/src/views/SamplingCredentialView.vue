<template>
  <section class="page credential-page" data-testid="credential-page">
    <header class="page-head">
      <div>
        <h2>采样凭据包 · 降采样复核材料</h2>
        <p class="page-desc">
          任务 {{ detail?.task.task_id ?? taskId }}：窗口左开右闭，分界点样本归右边一窗；
          材料锁定实际采用的采样轮次，正式签发后文件冻结，断线重领仍是同一文件。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn ghost" data-testid="reset-state" type="button" @click="onReset">
          重置演示状态
        </button>
        <RouterLink class="btn" to="/downsample">返回降采样列表</RouterLink>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</p>

    <template v-if="detail">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">源测点 / 粒度</span>
          <strong class="stat-value">{{ detail.task.source_point }} · {{ detail.task.granularity }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">复核时段</span>
          <strong class="stat-value">{{ rangeText }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">最新定稿轮次</span>
          <strong class="stat-value" data-testid="latest-round">
            {{ detail.latest_settled_round ? `第 ${detail.latest_settled_round} 轮` : '暂无' }}
          </strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">计算中轮次</span>
          <strong class="stat-value">
            {{ detail.open_round ? `第 ${detail.open_round.round_no} 轮` : '无' }}
          </strong>
        </article>
      </div>

      <div v-if="detail.waiting_reason" class="notice" data-testid="waiting-reason">
        <strong>等待原因：</strong>{{ detail.waiting_reason }}
      </div>

      <form class="analysis-form" data-testid="ingest-form" @submit.prevent="onIngest">
        <label>
          读数时间
          <input v-model="ingestTime" data-testid="ingest-time" required placeholder="2026-11-26T08:03:00+08:00" />
        </label>
        <label>
          读数值
          <input v-model.number="ingestValue" data-testid="ingest-value" type="number" step="0.0001" required />
        </label>
        <button class="btn primary" data-testid="ingest-submit" type="submit" :disabled="busy">
          接入读数（迟到将触发重算）
        </button>
        <button class="btn" data-testid="settle-round" type="button" :disabled="busy" @click="onSettle">
          定稿当前轮次
        </button>
        <button class="btn primary" data-testid="apply-package" type="button" :disabled="busy" @click="onApply">
          申请采样凭据包
        </button>
      </form>

      <h3>采样轮次（含本次实际采用的窗口与样本数）</h3>
      <table class="data-table" data-testid="rounds-table">
        <thead>
          <tr>
            <th>轮次</th>
            <th>状态</th>
            <th>窗口明细（序号 / 样本数 / 聚合值）</th>
            <th>定稿时间</th>
            <th>等待原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="round in detail.rounds" :key="round.round_no" :data-testid="`round-${round.round_no}`">
            <td>第 {{ round.round_no }} 轮</td>
            <td>{{ round.status }}</td>
            <td>
              <span v-if="!round.windows">计算未定稿</span>
              <ul v-else class="window-list">
                <li v-for="window in round.windows" :key="window.window_index">
                  窗{{ window.window_index }}（{{ shortTime(window.window_start) }} ~
                  {{ shortTime(window.window_end) }}）：{{ window.sample_count }} 个样本，
                  均值 {{ window.value === null ? '—' : window.value }}
                </li>
              </ul>
            </td>
            <td>{{ round.settled_at ?? '—' }}</td>
            <td>{{ round.reason ?? '—' }}</td>
          </tr>
          <tr v-if="!detail.rounds.length">
            <td colspan="5" class="empty-state">尚无采样轮次，请先接入读数</td>
          </tr>
        </tbody>
      </table>

      <h3>读数台账（时段外读数会被裁剪，不进入下载文件）</h3>
      <table class="data-table" data-testid="ledger-table">
        <thead>
          <tr>
            <th>时间戳</th>
            <th>读数值</th>
            <th>到达轮次</th>
            <th>归入窗口</th>
            <th>是否计入</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="reading in detail.readings_ledger" :key="reading.timestamp"
              :data-testid="`ledger-${reading.timestamp}`">
            <td>{{ reading.timestamp }}</td>
            <td>{{ reading.value }}</td>
            <td>第 {{ reading.arrived_round }} 轮</td>
            <td>{{ reading.window_index === null ? '时段外' : `窗 ${reading.window_index}` }}</td>
            <td>{{ reading.included ? '计入' : '裁剪' }}</td>
          </tr>
          <tr v-if="!detail.readings_ledger.length">
            <td colspan="5" class="empty-state">暂无读数</td>
          </tr>
        </tbody>
      </table>

      <h3>最新定稿快照（明细与聚合来自同一份计算）</h3>
      <template v-if="detail.latest_snapshot">
        <p data-testid="snapshot-round">
          本次实际采用：第 {{ detail.latest_snapshot.round_no }} 轮，
          时段外裁剪 {{ detail.latest_snapshot.excluded_count }} 条
        </p>
        <table class="data-table" data-testid="snapshot-windows">
          <thead>
            <tr>
              <th>窗口序号</th>
              <th>窗口起点</th>
              <th>窗口终点</th>
              <th>样本数</th>
              <th>聚合值</th>
              <th>样本时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="window in detail.latest_snapshot.windows" :key="window.window_index">
              <td>{{ window.window_index }}</td>
              <td>{{ window.window_start }}</td>
              <td>{{ window.window_end }}</td>
              <td>{{ window.sample_count }}</td>
              <td>{{ window.value === null ? '—' : window.value }}</td>
              <td>{{ window.samples.map((s) => shortTime(s.timestamp)).join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </template>
      <p v-else class="empty-state">还没有已定稿的完整采样轮次。</p>

      <h3>采样凭据包</h3>
      <table class="data-table" data-testid="packages-table">
        <thead>
          <tr>
            <th>凭据编号</th>
            <th>状态</th>
            <th>采用轮次</th>
            <th>申请时间</th>
            <th>签发时间 / 指纹</th>
            <th>等待原因</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pkg in detail.packages" :key="pkg.package_id"
              :data-testid="`package-${pkg.package_id}`" :data-status="pkg.status">
            <td>{{ pkg.package_id }}</td>
            <td>{{ pkg.status }}</td>
            <td>{{ pkg.adopted_round === null ? '暂无定稿轮次' : `第 ${pkg.adopted_round} 轮` }}</td>
            <td>{{ pkg.applied_at }}</td>
            <td>
              <div>{{ pkg.issued_at ?? '—' }}</div>
              <div class="fingerprint">{{ pkg.fingerprint ?? '—' }}</div>
            </td>
            <td>{{ pkg.pending_reason ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" data-testid="download-package" type="button" @click="onDownload(pkg.package_id)">
                下载材料
              </button>
              <button
                v-if="pkg.status === '草稿'"
                class="link"
                data-testid="issue-package"
                type="button"
                :disabled="pkg.adopted_round === null"
                @click="onIssue(pkg.package_id)"
              >
                正式签发
              </button>
            </td>
          </tr>
          <tr v-if="!detail.packages.length">
            <td colspan="7" class="empty-state">尚未申请凭据包</td>
          </tr>
        </tbody>
      </table>
    </template>

    <footer class="page-foot">
      <span>归窗规则：(窗口起点, 窗口终点]，正落在分界点的样本归右边一窗</span>
      <span v-if="notice" class="error-text">{{ notice }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  applyPackage,
  downloadPackage,
  fetchTaskDetail,
  ingestReading,
  issuePackage,
  resetState,
  settleRound,
} from '@/api/sampling-credential'
import type { TaskDetail } from '@/api/sampling-credential'

const props = defineProps<{ taskId?: string }>()
const taskId = props.taskId || 'DOWN-CRED-01'

const detail = ref<TaskDetail | null>(null)
const errorMessage = ref('')
const notice = ref('')
const busy = ref(false)
const ingestTime = ref('2026-11-26T08:03:00+08:00')
const ingestValue = ref<number>(15)

const rangeText = computed(() => {
  if (!detail.value) return ''
  return `${shortTime(detail.value.task.period_start)} ~ ${shortTime(detail.value.task.period_end)}`
})

function shortTime(ts: string): string {
  return ts.replace('2026-11-26T', '').replace('+08:00', '')
}

async function reload(message?: string) {
  try {
    detail.value = await fetchTaskDetail(taskId)
    notice.value = message ?? ''
    errorMessage.value = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

async function run(action: () => Promise<unknown>, okMessage: string) {
  busy.value = true
  try {
    const result = await action() as { message?: string }
    await reload(result.message || okMessage)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function onIngest() {
  void run(() => ingestReading(taskId, ingestTime.value, Number(ingestValue.value)), '读数已接入')
}

function onSettle() {
  void run(() => settleRound(taskId), '轮次已定稿')
}

function onApply() {
  void run(() => applyPackage(taskId), '凭据包已申请')
}

function onIssue(packageId: string) {
  void run(() => issuePackage(packageId), '凭据包已签发并冻结')
}

async function onDownload(packageId: string) {
  try {
    await downloadPackage(packageId)
    await reload('材料已下载（已签发文件与冻结内容一致）')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '下载失败'
  }
}

async function onReset() {
  busy.value = true
  try {
    await resetState()
    await reload('演示状态已重置')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重置失败'
  } finally {
    busy.value = false
  }
}

onMounted(() => void reload())
</script>

<style scoped>
.notice {
  background: #fff7ed;
  border: 1px solid #fdba74;
  color: #9a3412;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
.window-list {
  margin: 0;
  padding-left: 16px;
}
.fingerprint {
  font-family: monospace;
  font-size: 11px;
  color: var(--muted);
}
h3 {
  margin: 18px 0 8px;
  font-size: 15px;
}
</style>
