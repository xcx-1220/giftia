export const name = 'giftia-native-todo-access'

export function apply(ctx) {
  const base = process.env.GIFTIA_ROLE_URL
  const token = process.env.GIFTIA_ROLE_TOKEN
  const endpoint = base && new URL('/extension-access', base)
  if (endpoint && (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port)) {
    throw new Error('Native tool access must use the Giftia loopback service')
  }

  async function partner(agent, signal) {
    if (!endpoint || !token) return false
    const timeout = AbortSignal.timeout(5000)
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ session: String(agent.session.id) }),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      })
      return response.ok && (await response.json()).audience === 'partner'
    } catch {
      signal?.throwIfAborted()
      return false
    }
  }

  // Agent setup finishes before DSH resolves model-visible tools for the first step.
  ctx.on('agent/created', async ({ agent, signal }) => {
    if (!await partner(agent, signal)) agent.ctx.tools.restrict({ deny: ['todo_write'] })
  })
}
