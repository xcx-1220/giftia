import { createAssistantMessage, createSystemMessage, createUserMessage } from '@deepseek-ai/dsh-llm'

export const name = 'giftia-isla'
export const prompt = '你是艾拉。当前对话使用合成数据验证艾拉的 Harness profile。'

export function apply(ctx) {
  if (process.env.GIFTIA_ROLE_URL) {
    registerRole(ctx)
    console.error('[giftia-isla] role service loaded')
    return
  }
  ctx.systemPrompt.section({
    name: 'giftia:isla',
    order: 100,
    text: prompt,
    interpolate: false,
  })
  console.error('[giftia-isla] loaded')
}

function registerRole(ctx) {
  const endpoint = new URL(process.env.GIFTIA_ROLE_URL)
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port) {
    throw new Error('Giftia role service must use the designated loopback port')
  }
  const token = process.env.GIFTIA_ROLE_TOKEN
  if (!token) throw new Error('Missing Giftia role service token')
  const states = new WeakMap()

  async function call(path, session, payload, signal) {
    const response = await fetch(new URL(path, endpoint), {
      method: 'POST',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({ ...payload, session: String(session.id) }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
    })
    if (!response.ok) throw new Error(`Giftia role service rejected ${path} (${response.status})`)
    return response.json()
  }

  function load(agent, signal) {
    const session = agent.session
    let state = states.get(session)
    if (!state) {
      state = { step: 0, invalidated: false }
      state.ready = call('/prepare', session, {}, signal).then(context => { state.context = context })
      states.set(session, state)
    }
    return state.ready.then(() => state)
  }

  for (const { function: spec } of JSON.parse(process.env.GIFTIA_TOOL_SPECS)) {
    ctx.tools.register({
      ...spec,
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute(args, exec) {
        const state = await load(exec.agent, exec.signal)
        const result = await call('/tool', exec.agent.session, {
          name: spec.name, callId: exec.callId, args,
        }, exec.signal)
        state.invalidated ||= result.invalidate
        return result.text
      },
    }, {
      async when({ agent, signal }) {
        const state = await load(agent, signal)
        return !state.invalidated && state.step + 1 < state.context.rounds && state.context.tools.includes(spec.name)
      },
    })
  }

  ctx.systemPrompt.section({
    name: 'giftia:isla', order: 100, interpolate: false, complete: true,
    text({ agent }) {
      if (!agent) return ''
      const state = states.get(agent.session)
      if (!state?.context) throw new Error('Giftia context was not prepared')
      return rolePrompt(state)
    },
  })

  ctx.on('agent/pre-step', async ({ agent, turn, step, modelSelection, signal }, next) => {
    const state = await load(agent, signal)
    if (state.invalidated || step > state.context.rounds) return { kind: 'reject' }
    if (step > 1) {
      const { invalidated } = await call('/authorize', agent.session, {}, signal)
      if (invalidated) return { kind: 'reject' }
    }
    if (step === 1 && state.context.history.length) {
      // The loop's first system node would otherwise follow the restored history.
      const system = createSystemMessage(rolePrompt(state))
      agent.session.append('system/message', { turn, step, message: system }, { surfaceOp: 'append' })
      for (const item of state.context.history) {
        const content = [{ type: 'text', text: item.content }]
        if (item.role === 'user') {
          const message = createUserMessage({ content, source: { kind: 'user' } })
          agent.session.append('user/message', message, { surfaceOp: 'append' })
        } else if (item.role === 'assistant') {
          const message = createAssistantMessage({ content, source: { provider: modelSelection.provider, model: modelSelection.model } })
          agent.session.append('assistant/message', { turn, step, message, stream: [] }, { surfaceOp: 'append' })
        }
      }
    }
    state.step = step
    return next()
  })

  // The locked SDK exposes maxTokens but no temperature argument. At rc.3 this
  // hook is the only call-control override that keeps the logged header accurate.
  ctx.on('agent/request', async ({ agent, signal }, next) => {
    const state = await load(agent, signal)
    return { ...await next(), temperature: state.context.temperature, maxTokens: state.context.maxTokens }
  })
}

function rolePrompt(state) {
  const { system, history, note, rounds, lastRound } = state.context
  const lines = [system, ...history.filter(item => item.role === 'system').map(item => item.content)]
  if (lastRound && state.step + 1 >= rounds) lines.push(lastRound)
  if (note) lines.push(note)
  return lines.join('\n\n')
}
