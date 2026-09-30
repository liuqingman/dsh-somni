/**
 * System-2 memory tools: the sidecar's `tools.list` is mirrored onto
 * `ctx.tools` so the model can deliberately recall episodes / knowledge,
 * search the current conversation, and manage intentions. Schemas come from
 * Python docstrings (see `somni_memory/toolspec.py`); the TS side only
 * forwards calls and refreshes the resident prompt after intention changes.
 */
import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import type { Sidecar } from './sidecar.ts'
import type { PromptSections } from './prompt.ts'

interface RemoteTool {
  name: string
  description: string
  parameters: Record<string, unknown>
}

/** Tools whose success changes what the resident prompt should show. */
const MUTATES_INTENTIONS = new Set(['add_intention', 'complete_intention', 'cancel_intention'])
const TOOL_TIMEOUT_MS = 30_000

export function registerMemoryTools(ctx: Context, sidecar: Sidecar, prompt: PromptSections): void {
  const logger = ctx.logger('somni:tools')
  const userId = process.env['USER'] || process.env['USERNAME'] || 'default'
  let registered = false

  const register = async () => {
    if (registered) return
    const { tools } = await sidecar.call<{ tools: RemoteTool[] }>('tools.list', {}, 15_000)
    if (registered) return
    registered = true
    for (const tool of tools) {
      const definition: ToolDefinition = {
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters,
        output: {
          schema: { type: 'string' },
          render: (_args, value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) }],
        },
        isConcurrencySafe: () => !MUTATES_INTENTIONS.has(tool.name),
        timeoutMs: TOOL_TIMEOUT_MS,
        async execute(args, exec) {
          const r = await sidecar.call<{ result: unknown }>('tools.call', {
            name: tool.name,
            args: (args as Record<string, unknown>) ?? {},
            sessionId: exec.agent?.id ?? '',
            userId,
          }, TOOL_TIMEOUT_MS)
          if (MUTATES_INTENTIONS.has(tool.name)) void prompt.refresh()
          return typeof r.result === 'string' ? r.result : JSON.stringify(r.result ?? '')
        },
      }
      ctx.tools.register(definition)
    }
    logger.info('registered %d memory tools: %s', tools.length, tools.map((t) => t.name).join(', '))
  }

  sidecar.start()
    .then(register)
    .catch((err: Error) => logger.warn('memory tools unavailable: %s', err.message))
}
