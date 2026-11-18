export const name = 'giftia-chat-tools'

const operations = {
  add: (left, right) => left + right,
  subtract: (left, right) => left - right,
  multiply: (left, right) => left * right,
  divide: (left, right) => left / right,
  percent_of: (left, right) => (left / 100) * right,
  power: (left, right) => left ** right,
}

const units = {
  mm: ['length', 0.001], cm: ['length', 0.01], m: ['length', 1], km: ['length', 1000],
  in: ['length', 0.0254], ft: ['length', 0.3048], mi: ['length', 1609.344],
  mg: ['mass', 0.000001], g: ['mass', 0.001], kg: ['mass', 1], lb: ['mass', 0.45359237],
  mL: ['volume', 0.001], L: ['volume', 1],
  s: ['time', 1], min: ['time', 60], h: ['time', 3600], day: ['time', 86400],
  C: ['temperature', 1, 0], F: ['temperature', 5 / 9, -32], K: ['temperature', 1, -273.15],
}

function decimal(value) {
  if (!Number.isFinite(value)) throw new Error('计算结果超出范围，或该运算在实数范围内没有结果。')
  return Number(value.toPrecision(12))
}

function calculate({ operation, left, right }) {
  if (!Object.hasOwn(operations, operation) || ![left, right].every(Number.isFinite)) {
    throw new Error('请提供受支持的运算和两个有限数值。')
  }
  if (operation === 'divide' && right === 0) throw new Error('除数不能是零。')
  return String(decimal(operations[operation](left, right)))
}

function convert({ value, from, to }) {
  if (!Number.isFinite(value) || !Object.hasOwn(units, from) || !Object.hasOwn(units, to)) {
    throw new Error('请提供有限数值和受支持的单位。')
  }
  const [kind, inputFactor, inputOffset = 0] = units[from]
  const [targetKind, outputFactor, outputOffset = 0] = units[to]
  if (kind !== targetKind) throw new Error('两种单位表示不同的量，不能直接换算。')
  const base = (value + inputOffset) * inputFactor
  if (kind === 'temperature' && base < -273.1500000001) throw new Error('温度低于绝对零度。')
  return `${decimal(base / outputFactor - outputOffset)} ${to}`
}

const unit = { type: 'string', enum: Object.keys(units) }
const tools = [
  {
    name: 'isla_calculate',
    description: '计算两个数的加、减、乘、除、乘方，或求 right 的 left%。适用于用户需要的数值计算。',
    parameters: {
      type: 'object', properties: {
        operation: { type: 'string', enum: Object.keys(operations) },
        left: { type: 'number' }, right: { type: 'number' },
      }, required: ['operation', 'left', 'right'], additionalProperties: false,
    },
    run: calculate,
  },
  {
    name: 'isla_convert_units',
    description: '换算同类单位：长度、质量、体积、时间和温度。C、F、K 表示摄氏、华氏和开尔文。',
    parameters: {
      type: 'object', properties: { value: { type: 'number' }, from: unit, to: unit },
      required: ['value', 'from', 'to'], additionalProperties: false,
    },
    run: convert,
  },
]

export function apply(ctx, config) {
  const url = process.env.GIFTIA_ROLE_URL
  const token = process.env.GIFTIA_ROLE_TOKEN
  const permissions = config?.permissions
  if (!url || !token || !(permissions?.partner === true || permissions?.guest === true)) return
  const endpoint = new URL('/extension-access', url)
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port) {
    throw new Error('Extension access must use the role service loopback port')
  }

  const steps = new WeakMap()
  ctx.on('agent/pre-step', ({ agent, step }, next) => {
    steps.set(agent.session, step)
    return next()
  })

  async function authorized(agent, signal, step) {
    if (!agent) return false
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

  for (const { run, ...spec } of tools) {
    ctx.tools.register({
      ...spec,
      output: {
        schema: { type: 'string' },
        render: (_args, text) => [{ type: 'text', text }],
      },
      async execute(args, { agent, signal }) {
        if (!agent || !await authorized(agent, signal, steps.get(agent.session))) {
          throw new Error('Chat tool is outside the active authorized view')
        }
        return run(args)
      },
    }, {
      when({ agent, signal }) {
        return authorized(agent, signal, (steps.get(agent.session) ?? 0) + 1)
      },
    })
  }
}
