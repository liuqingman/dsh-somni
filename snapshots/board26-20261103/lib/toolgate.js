import { somniDisabled } from "./gate.js";
/**
 * Tool-face gate: mirror the dsh-somni soft hot-switch onto the DSH tool
 * registry. While the soft-switch is OFF, gated tool schemas are unmounted
 * (they disappear from tools/list, so the LLM cannot even see them); on ON
 * they are re-mounted. All dsh-somni tool registrations go through
 * gatedRegister(); a 5s watcher flips the whole gated set on state change.
 * The execute()-side hard reject (kept in each tool) remains as a
 * belt-and-suspenders fallback for the in-flight window right after a flip.
 */
const WATCH_INTERVAL_MS = 5000;
const defs = new Map(); // tool name -> definition
const disposers = new Map(); // tool name -> unregister disposer
let gateCtx = null;
let gateLogger = null;
let watcher = null;
let mounted = false;

function mountAll() {
    if (!gateCtx) return;
    for (const [name, def] of defs) {
        if (disposers.has(name)) continue;
        try {
            disposers.set(name, gateCtx.tools.register(def));
            gateLogger.info('tool-face gate: "%s" mounted', name);
        } catch (e) {
            gateLogger.warn('tool-face gate: mount "%s" failed: %s', name, e && e.message);
        }
    }
    mounted = disposers.size > 0;
}

function unmountAll(reason) {
    for (const [name, dispose] of disposers) {
        try { dispose(); gateLogger.info('tool-face gate: "%s" unmounted', name); }
        catch (e) { gateLogger.warn('tool-face gate: unmount "%s" failed: %s', name, e && e.message); }
    }
    disposers.clear();
    mounted = false;
    if (reason) gateLogger.info('tool-face gate: all gated tools unmounted (%s)', reason);
}

export function gatedRegister(ctx, definition, logger) {
    gateCtx = ctx;
    gateLogger = logger || ctx.logger('somni:toolgate');
    defs.set(definition.name, definition);
    if (somniDisabled()) {
        gateLogger.info('tool-face gate: "%s" registered but held back (soft-switch OFF)', definition.name);
    } else {
        try {
            disposers.set(definition.name, ctx.tools.register(definition));
            mounted = true;
        } catch (e) {
            gateLogger.warn('tool-face gate: initial mount "%s" failed: %s', definition.name, e && e.message);
        }
    }
    if (!watcher) {
        watcher = setInterval(() => {
            const disabled = somniDisabled();
            if (disabled && mounted) unmountAll('soft-switch OFF');
            else if (!disabled && !mounted && defs.size) {
                mountAll();
                gateLogger.info('tool-face gate: %d tool(s) re-mounted (soft-switch ON)', disposers.size);
            }
        }, WATCH_INTERVAL_MS);
        if (watcher.unref) watcher.unref();
    }
}

export function stopToolGate() {
    if (watcher) { clearInterval(watcher); watcher = null; }
    if (mounted) unmountAll('plugin teardown');
    defs.clear();
}
