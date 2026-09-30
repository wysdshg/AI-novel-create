// SSE 流式读取工具 —— 统一 4 处重复实现（Phase 3.3，2026-09-10）。
//
// 改造前 `chapter.js` / `discussion.js`（2 处）/ `workflow.js` 各自手写了一段
// 「getReader + TextDecoder + split('\n\n') + 正则抠 event/data」的循环，
// 差异只在**错误容忍度**与**超时/中止**处理——正是这种"看着差不多"的复制
// 最容易在修 bug 时漏改其中一处。
//
// 本模块提供两层：
//   - `createSseParser(onEvent)`：纯解析器，喂 chunk 出去事件，可单测；
//   - `readSseStream(resp, onEvent, opts)`：在解析器之上封装 fetch 响应读取 +
//     超时/外部中止 + `GenerationStopped` 语义。

/** 默认超时：与 axios timeout 一致（云端 LLM 常需 30~90s） */
// ⚠️ 语义是「**空闲**超时」——距上一次收到数据的间隔，不是整条流的总时长。
// 2026-09-20 修正：原实现按总时长计时，而 Qwen3.8-Flash-Next 开思考首字实测 407s，
// 即便后端已在持续推送 thinking 保活帧，180s 一到仍会被误杀。改为空闲计时后，
// 只要流还在动（哪怕只是思考帧）就不算超时，只有真·卡死才会触发。
export const SSE_TIMEOUT_MS = 180000

/**
 * 创建 SSE 帧解析器。SSE 帧以空行分隔，可能被网络分片切断，故需跨 chunk 缓冲。
 *
 * @param {(event: string, data: any) => void} onEvent 事件回调
 * @param {{tolerant?: boolean}} [opts]
 *   `tolerant=true`：单条 data 不是合法 JSON 时跳过该条（默认，用于长流不中断）；
 *   `tolerant=false`：直接抛出（用于本地工作流，错误应尽早暴露）。
 * @returns {{push: (chunk: string) => void, flush: () => void}}
 */
export function createSseParser(onEvent, { tolerant = true } = {}) {
  let buffer = ''

  const handleBlock = (block) => {
    const ev = /event: (.+)/.exec(block)
    const data = /data: (.+)/.exec(block)
    if (!ev || !data) return
    if (tolerant) {
      try {
        onEvent?.(ev[1], JSON.parse(data[1]))
      } catch {
        /* 忽略单条畸形 SSE 数据，避免杀死整条流 */
      }
    } else {
      onEvent?.(ev[1], JSON.parse(data[1]))
    }
  }

  return {
    /** 喂入一个解码后的文本分片 */
    push(chunk) {
      buffer += chunk
      const blocks = buffer.split('\n\n')
      // 最后一段可能是不完整帧，留到下次
      buffer = blocks.pop() || ''
      for (const block of blocks) handleBlock(block)
    },
    /** 流结束时把缓冲区剩余内容也解析掉（有些后端末帧不带结尾空行） */
    flush() {
      if (!buffer) return
      const rest = buffer
      buffer = ''
      for (const block of rest.split('\n\n')) {
        if (block.trim()) handleBlock(block)
      }
    },
  }
}

/**
 * 创建带超时的 AbortController；超时自动 abort。
 *
 * 计时语义为**空闲超时**：`reset()` 会把计时器重新拨满，只要流还在推数据就不算超时。
 *
 * @returns {{controller: AbortController, reset: () => void, cleanup: () => void}}
 */
export function createTimeoutController(ms = SSE_TIMEOUT_MS) {
  const controller = new AbortController()
  let timer = setTimeout(() => controller.abort(), ms)
  return {
    controller,
    /** 收到任意数据后调用：重新拨满空闲计时器 */
    reset: () => {
      clearTimeout(timer)
      timer = setTimeout(() => controller.abort(), ms)
    },
    cleanup: () => clearTimeout(timer),
  }
}

/**
 * 校验响应并读取其 SSE 流。
 *
 * 调用方需自行完成 `fetch`（各接口 method/body/header 不同），把响应交给本函数。
 * 本函数负责：HTTP 状态校验 → 读流解析 → 超时/中止语义。
 *
 * @param {Response} resp
 * @param {(event: string, data: any) => void} onEvent
 * @param {{
 *   signal?: AbortSignal,      外部中止信号（用户点"停止生成"）
 *   timeoutMs?: number,        读流超时（默认 SSE_TIMEOUT_MS）
 *   tolerant?: boolean,        SSE 数据容忍度，透传 createSseParser
 *   errorPrefix?: string,      非 2xx 时的错误文案前缀，如 "商讨接口"
 *   stoppedMessage?: string,   中止时的错误消息（name 恒为 GenerationStopped）
 * }} [opts]
 */
export async function readSseStream(resp, onEvent, {
  signal,
  timeoutMs = SSE_TIMEOUT_MS,
  tolerant = true,
  errorPrefix = '接口',
  stoppedMessage = '已停止生成',
} = {}) {
  // ⚠️ 必须显式校验 HTTP 状态：后端在 400/500 时返回的是 JSON 错误体（不是 SSE），
  // 不校验就会拿它当事件流解析 → 前端只看到"完全没有输出"，错误信息被吞掉（静默失败）。
  if (!resp.ok) {
    let detail = ''
    try {
      const j = await resp.json()
      detail = j?.detail || j?.message || ''
    } catch {
      /* 错误体不是 JSON 就忽略，用状态码兜底 */
    }
    throw new Error(detail || `${errorPrefix}返回 ${resp.status}`)
  }
  if (!resp.body) {
    throw new Error(`${errorPrefix}未返回数据流`)
  }

  const { controller, reset, cleanup } = createTimeoutController(timeoutMs)
  // 外部 signal 与超时 controller 合并：任一触发即中止
  const onExternalAbort = () => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', onExternalAbort, { once: true })
  }

  // 注意：resp 已由调用方 fetch（用的是它自己的 signal），这里只能通过
  // reader.cancel() 响应中止——否则外部 signal 只 abort 了"读"而没断"连"。
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  const parser = createSseParser(onEvent, { tolerant })

  // 🔴 2026-09-20 修复：此前 controller 造出来后**没有任何消费者**——
  // 既没传给 fetch（fetch 用的是外部 signal），reader.read() 又不接受 signal，
  // 于是定时器到点后 controller.abort() 什么也没发生，180s 超时保护形同虚设。
  // 现在显式监听 abort → reader.cancel()，让挂起的 read() 立刻结束；
  // 再用 timedOut 区分「超时」与「用户手动停止」，两者给出各自明确的错误文案。
  let timedOut = false
  const onTimeoutAbort = () => {
    // 外部 signal 已中止 = 用户主动停止，不算超时
    if (!signal || !signal.aborted) timedOut = true
    reader.cancel().catch(() => { /* 已结束的流无需处理 */ })
  }
  controller.signal.addEventListener('abort', onTimeoutAbort, { once: true })

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      // 收到数据 = 流还活着，把空闲计时器拨满（见 SSE_TIMEOUT_MS 的语义说明）
      reset()
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.flush()
  } catch (e) {
    if (e?.name === 'AbortError') {
      const err = new Error(stoppedMessage)
      err.name = 'GenerationStopped'
      throw err
    }
    throw e
  } finally {
    controller.signal.removeEventListener('abort', onTimeoutAbort)
    if (signal) signal.removeEventListener('abort', onExternalAbort)
    cleanup()
  }

  // 超时判定放在 try/finally **之后**：放在 finally 里抛会掩盖内部的 GenerationStopped
  if (timedOut) {
    throw new Error(`${errorPrefix}超时（连续 ${Math.round(timeoutMs / 1000)} 秒未收到数据），请重试或换一个响应更快的模型`)
  }
}

/**
 * 一站式：POST JSON 并按 SSE 读取。适合 method/header 完全一致的场景。
 *
 * @param {string} url
 * @param {object} body
 * @param {(event: string, data: any) => void} onEvent
 * @param {object} [opts] 透传 readSseStream；另有 `signal` 同时用于 fetch 与读取
 */
export async function postSseStream(url, body, onEvent, opts = {}) {
  const { signal, ...rest } = opts
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  return readSseStream(resp, onEvent, { signal, ...rest })
}
