/**
 * Host-LLM bridge: serves the sidecar's reverse `llm.chat` request by running
 * the dream's ReAct step on the harness model (`ctx.llm.stream`) with the
 * agent's current default selection. OpenAI-style messages/tools in,
 * OpenAI-style assistant message + usage out — the Python side never learns
 * which provider is behind it.
 */
import type { Context } from '@deepseek-ai/cordis';
interface OaiToolCall {
    id: string;
    type: 'function';
    function: {
        name: string;
        arguments: string;
    };
}
interface OaiMessage {
    role: 'system' | 'user' | 'assistant' | 'tool';
    content?: string | null;
    reasoning_content?: string;
    tool_calls?: OaiToolCall[];
    tool_call_id?: string;
}
interface OaiTool {
    type: 'function';
    function: {
        name: string;
        description?: string;
        parameters?: Record<string, unknown>;
    };
}
export interface ChatParams {
    messages: OaiMessage[];
    tools?: OaiTool[];
}
export interface ChatResult {
    message: OaiMessage;
    usage: {
        prompt_tokens: number;
        completion_tokens: number;
        cached_tokens?: number;
    };
}
/** Build the `llm.chat` handler bound to a harness context. */
export declare function hostChat(ctx: Context): (params: ChatParams, signal?: AbortSignal) => Promise<ChatResult>;
export {};
