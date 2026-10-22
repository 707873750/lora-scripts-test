// @vitest-environment jsdom
import { flushPromises, mount } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import QueueShutdownControl from "./QueueShutdownControl.vue"
import { tasksApi, type QueueShutdownState } from "../api/tasks"

vi.mock("../api/tasks", () => ({ tasksApi: { getShutdown: vi.fn(), setShutdown: vi.fn() } }))
const initial: QueueShutdownState = { supported: true, enabled: false, remaining_seconds: null, delay_seconds: 60, triggered: false, error: null }

beforeEach(() => {
  vi.useFakeTimers()
  vi.mocked(tasksApi.getShutdown).mockResolvedValue({ shutdown: { ...initial } })
})
afterEach(() => {
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe("queue shutdown controls", () => {
  it("enables shutdown and lets the user cancel the countdown", async () => {
    const wrapper = mount(QueueShutdownControl)
    await flushPromises()
    vi.mocked(tasksApi.setShutdown).mockResolvedValueOnce({ shutdown: { ...initial, enabled: true, remaining_seconds: 60 } })
    await wrapper.get('input').setValue(true)
    await flushPromises()
    expect(tasksApi.setShutdown).toHaveBeenCalledWith(true)
    expect(wrapper.text()).toContain("60 秒后关闭训练服务器")
    vi.mocked(tasksApi.setShutdown).mockResolvedValueOnce({ shutdown: initial })
    await wrapper.get('button').trigger("click")
    await flushPromises()
    expect(tasksApi.setShutdown).toHaveBeenLastCalledWith(false)
    expect(wrapper.text()).toContain("自动关机已关闭")
    wrapper.unmount()
  })

  it("ignores an old poll that returns after cancellation", async () => {
    let resolvePoll!: (data: { shutdown: QueueShutdownState }) => void
    vi.mocked(tasksApi.getShutdown).mockResolvedValueOnce({ shutdown: { ...initial, enabled: true, remaining_seconds: 60 } })
    const wrapper = mount(QueueShutdownControl)
    await flushPromises()
    vi.mocked(tasksApi.getShutdown).mockImplementationOnce(() => new Promise((resolve) => { resolvePoll = resolve }))
    await vi.advanceTimersByTimeAsync(2000)
    vi.mocked(tasksApi.setShutdown).mockResolvedValue({ shutdown: initial })
    await wrapper.get('button').trigger('click')
    expect(tasksApi.setShutdown).toHaveBeenCalledWith(false)
    await flushPromises()
    resolvePoll({ shutdown: { ...initial, enabled: true, remaining_seconds: 1 } })
    await flushPromises()
    expect(wrapper.text()).toContain("自动关机已关闭")
    expect((wrapper.get('input').element as HTMLInputElement).checked).toBe(false)
    wrapper.unmount()
  })

  it("shows command errors and stops polling when unmounted", async () => {
    const wrapper = mount(QueueShutdownControl)
    await flushPromises()
    vi.mocked(tasksApi.setShutdown).mockRejectedValue(new Error("权限不足"))
    await wrapper.get('input').setValue(true)
    await flushPromises()
    expect(wrapper.text()).toContain("权限不足")
    expect((wrapper.get('input').element as HTMLInputElement).checked).toBe(false)
    wrapper.unmount()
    const count = vi.mocked(tasksApi.getShutdown).mock.calls.length
    await vi.advanceTimersByTimeAsync(4000)
    expect(tasksApi.getShutdown).toHaveBeenCalledTimes(count)
  })
})
