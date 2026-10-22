<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { tasksApi, type QueueShutdownState } from "../api/tasks"

const state = ref<QueueShutdownState | null>(null)
const busy = ref(false)
const error = ref("")
let revision = 0
let polling = false
let disposed = false
let timer: ReturnType<typeof setInterval> | undefined

const message = computed(() => {
  if (error.value) return error.value
  if (!state.value) return "正在读取自动关机设置…"
  if (state.value.error) return state.value.error
  if (!state.value.supported) return "当前平台不支持自动关机"
  if (state.value.triggered) return "已发送关机命令"
  if (state.value.remaining_seconds != null) return `所有任务已结束，${state.value.remaining_seconds} 秒后关闭训练服务器。`
  return state.value.enabled ? "队列结束后倒计时 60 秒关机；新任务会撤销倒计时。" : "自动关机已关闭"
})

async function refresh() {
  if (polling || busy.value) return
  polling = true
  const current = revision
  try {
    const data = await tasksApi.getShutdown()
    if (!disposed && current === revision) {
      state.value = data.shutdown
      error.value = ""
    }
  } catch (caught) {
    if (!disposed && current === revision) error.value = caught instanceof Error ? caught.message : "读取自动关机设置失败"
  } finally {
    polling = false
  }
}

async function save(enabled: boolean) {
  if (busy.value) return
  busy.value = true
  revision++
  try {
    const data = await tasksApi.setShutdown(enabled)
    if (!disposed) {
      state.value = data.shutdown
      error.value = ""
    }
  } catch (caught) {
    if (!disposed) error.value = caught instanceof Error ? caught.message : "保存自动关机设置失败"
  } finally {
    busy.value = false
    revision++
  }
}

function changeEnabled(event: Event) {
  const checkbox = event.target as HTMLInputElement
  const enabled = checkbox.checked
  checkbox.checked = state.value?.enabled ?? false
  void save(enabled)
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), 2000)
})
onBeforeUnmount(() => {
  disposed = true
  clearInterval(timer)
})
</script>

<template>
  <section class="queue-shutdown-control" :class="{ 'is-counting-down': state?.remaining_seconds != null }" aria-label="训练队列自动关机">
    <label>
      <input type="checkbox" :checked="state?.enabled ?? false" :disabled="busy || !state?.supported || state?.triggered" @change="changeEnabled">
      所有任务结束后自动关机
    </label>
    <span role="status">{{ message }}</span>
    <button v-if="state?.remaining_seconds != null" class="ghost-button" :disabled="busy" @click="save(false)">取消自动关机</button>
    <button v-if="error" class="ghost-button" :disabled="busy" @click="refresh">重试</button>
  </section>
</template>
