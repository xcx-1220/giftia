export const name = 'giftia-sample-tool'

const REFERENCE = '合成资料 SYNTH-REF-001：星砂图书室每周三 14:00 开放。'

export function apply(ctx, config) {
  const url = process.env.GIFTIA_ROLE_URL
  const token = process.env.GIFTIA_ROLE_TOKEN
  if (!url || !token) return
  const endpoint = new URL('/extension-access', url)
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port) {
    throw new Error('Extension access must use the role service loopback port')
  }

  const steps = new WeakMap()
  const permissions = config?.permissions
  ctx.on('agent/pre-step', ({ agent, step }, next) => {
    steps.set(agent.session, step)
    return next()
  })

  async function authorized(agent, signal, step) {
    if (!agent || !(permissions?.partner === true || permissions?.guest === true)) return false
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
        body: JSON.stringify({ session: String(agent.session.id) }),
        signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
      })
      if (!response.ok) return false
      const { audience, rounds } = await response.json()
      return (audience === 'partner' || audience === 'guest') && permissions[audience] === true && step < rounds
    } catch {
      signal.throwIfAborted()
      return false
    }
  }

  ctx.tools.register({
    name: 'sample_reference',
    description: '读取星砂图书室的固定合成开放时间。',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'string' },
      render: (_args, text) => [{ type: 'text', text }],
    },
    async execute(_args, { agent, signal }) {
      if (!agent || !await authorized(agent, signal, steps.get(agent.session))) {
        throw new Error('Sample reference is outside the active authorized view')
      }
      return REFERENCE
    },
  }, {
    when({ agent, signal }) {
      // Harness resolves the tool set before entering the next step.
      return authorized(agent, signal, (steps.get(agent.session) ?? 0) + 1)
    },
  })
}
