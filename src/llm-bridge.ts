/**
 * Host-LLM bridge: serves the sidecar's reverse `llm.chat` request by running
 * the dream's ReAct step on the harness model (`ctx.llm.stream`) with the
 * agent's current default selection. OpenAI-style messages/tools in,
 * OpenAI-style assistant message + usage out — the Python side never learns
 * which provider is behind it.
 */
import type { Context } from '@deepseek-ai/cordis'
import {
  createAssistantMessage, createToolResultMessage,
  type ContentBlock, type RequestMessage, type ToolCallId, type ToolSchema,
} from '@deepseek-ai/dsh-llm'

interface OaiToolCall {
  id: string
  type: 'function'
  function: { name: string; arguments: string }
}

interface OaiMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content?: string | null
  reasoning_content?: string
  tool_calls?: OaiToolCall[]
  tool_call_id?: string
}

interface OaiTool {
  type: 'function'
  function: { name: string; description?: string; parameters?: Record<string, unknown> }
}

export interface ChatParams {
  messages: OaiMessage[]
  tools?: OaiTool[]
}

export interface ChatResult {
  message: OaiMessage
  usage: { prompt_tokens: number; completion_tokens: number; cached_tokens?: number }
}

function text(t: string): ContentBlock {
  return { type: 'text', text: t }
}

function toRequest(messages: OaiMessage[], provider: string, model: string): { system: string | undefined; messages: RequestMessage[] } {
  const systems: string[] = []
  const out: RequestMessage[] = []
  for (const m of messages) {
    const body = m.content ?? ''
    switch (m.role) {
      case 'system':
        if (body) systems.push(body)
        break
      case 'user':
        out.push({ role: 'user', content: [text(body)] })
        break
      case 'assistant': {
        const content: ContentBlock[] = []
        if (m.reasoning_content) content.push({ type: 'reasoning', text: m.reasoning_content })
        if (body) content.push(text(body))
        for (const tc of m.tool_calls ?? []) {
          content.push({ type: 'tool-call', id: tc.id as ToolCallId, name: tc.function.name, arguments: tc.function.arguments })
        }
        out.push(createAssistantMessage({ content, source: { provider, model } }))
        break
      }
      case 'tool':
        out.push(createToolResultMessage({
          callId: (m.tool_call_id ?? '') as ToolCallId,
          content: [text(body)],
          isError: false,
        }))
        break
    }
  }
  return { system: systems.length ? systems.join('\n\n') : undefined, messages: out }
}

function toToolSchemas(tools: OaiTool[] | undefined): ToolSchema[] | undefined {
  if (!tools?.length) return undefined
  return tools.map((t) => ({
    name: t.function.name,
    description: t.function.description ?? '',
    parameters: t.function.parameters ?? { type: 'object', properties: {} },
  }))
}

/** Build the `llm.chat` handler bound to a harness context. */
export function hostChat(ctx: Context) {
  return async (params: ChatParams, signal?: AbortSignal): Promise<ChatResult> => {
    const selection = ctx.agentDefaultModel.currentSelection()
    const { system, messages } = toRequest(params.messages, selection.provider, selection.model)

    // Assemble by completed blocks; fall back to deltas if a provider skips block-end.
    const blocks = new Map<number, ContentBlock>()
    const textDelta = new Map<number, string>()
    const reasoningDelta = new Map<number, string>()
    const callDelta = new Map<number, { id: string; name: string; args: string }>()
    let usage: ChatResult['usage'] = { prompt_tokens: 0, completion_tokens: 0 }

    const stream = ctx.llm.stream({
      provider: selection.provider,
      model: selection.model,
      ...(selection.reasoningEffort ? { reasoningEffort: selection.reasoningEffort } : {}),
      messages,
      ...(system ? { system } : {}),
      ...(toToolSchemas(params.tools) ? { tools: toToolSchemas(params.tools) } : {}),
      ...(signal ? { signal } : {}),
    })
    for await (const chunk of stream) {
      switch (chunk.type) {
        case 'text-delta':
          textDelta.set(chunk.index, (textDelta.get(chunk.index) ?? '') + chunk.text)
          break
        case 'reasoning-delta':
          reasoningDelta.set(chunk.index, (reasoningDelta.get(chunk.index) ?? '') + chunk.text)
          break
        case 'tool-call-delta': {
          const cur = callDelta.get(chunk.index) ?? { id: chunk.id, name: '', args: '' }
          if (chunk.name) cur.name = chunk.name
          cur.args += chunk.argumentsDelta
          callDelta.set(chunk.index, cur)
          break
        }
        case 'block-end':
          blocks.set(chunk.index, chunk.block)
          break
        case 'usage':
          usage = {
            prompt_tokens: chunk.usage.inputTokens,
            completion_tokens: chunk.usage.outputTokens,
            ...(chunk.usage.cacheReadTokens !== undefined ? { cached_tokens: chunk.usage.cacheReadTokens } : {}),
          }
          break
        default:
          break
      }
    }

    const indices = new Set<number>([...blocks.keys(), ...textDelta.keys(), ...reasoningDelta.keys(), ...callDelta.keys()])
    const texts: string[] = []
    const reasonings: string[] = []
    const toolCalls: OaiToolCall[] = []
    for (const i of [...indices].sort((a, b) => a - b)) {
      const block = blocks.get(i)
      if (block) {
        if (block.type === 'text') texts.push(block.text)
        else if (block.type === 'reasoning') reasonings.push(block.text)
        else if (block.type === 'tool-call') {
          toolCalls.push({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } })
        }
        continue
      }
      const t = textDelta.get(i)
      if (t !== undefined) texts.push(t)
      const r = reasoningDelta.get(i)
      if (r !== undefined) reasonings.push(r)
      const c = callDelta.get(i)
      if (c) toolCalls.push({ id: c.id, type: 'function', function: { name: c.name, arguments: c.args || '{}' } })
    }

    const message: OaiMessage = { role: 'assistant', content: texts.join('') }
    if (reasonings.length) message.reasoning_content = reasonings.join('')
    if (toolCalls.length) message.tool_calls = toolCalls
    return { message, usage }
  }
}
