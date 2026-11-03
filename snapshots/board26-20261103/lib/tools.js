import { somniDisabled } from "./gate.js";
import { gatedRegister } from "./toolgate.js";
/** Tools whose success changes what the resident prompt should show. */
const MUTATES_INTENTIONS = new Set(['add_intention', 'complete_intention', 'cancel_intention']);
const TOOL_TIMEOUT_MS = 30_000;
export function registerMemoryTools(ctx, sidecar, prompt) {
    const logger = ctx.logger('somni:tools');
    const userId = process['env']['USER'] || process['env']['USERNAME'] || 'default';
    let registered = false;
    const register = async () => {
        if (registered)
            return;
        const { tools } = await sidecar.call('tools.list', {}, 15_000);
        if (registered)
            return;
        registered = true;
        for (const tool of tools) {
            const definition = {
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
                    if (somniDisabled())
                        return 'dsh-somni is currently disabled (soft hot-switch); memory tools are unavailable.';
                    const r = await sidecar.call('tools.call', {
                        name: tool.name,
                        args: args ?? {},
                        sessionId: exec.agent?.id ?? '',
                        userId,
                    }, TOOL_TIMEOUT_MS);
                    if (MUTATES_INTENTIONS.has(tool.name))
                        void prompt.refresh();
                    return typeof r.result === 'string' ? r.result : JSON.stringify(r.result ?? '');
                },
            };
            gatedRegister(ctx, definition, logger);
        }
        logger.info('registered %d memory tools: %s (tool-face gate active)', tools.length, tools.map((t) => t.name).join(', '));
    };
    sidecar.start()
        .then(register)
        .catch((err) => logger.warn('memory tools unavailable: %s', err.message));
}
