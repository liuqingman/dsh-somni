# board26 runtime snapshot (2026-11-03)

Board `172.16.0.26:/xdata/dsh/market/dsh-somni/package` as found live on 2026-11-03.

## Why this exists
The board copy of `lib/*.js` diverged from this repo: several files were hand-edited
in place on the board (no matching `src/*.ts`), so a rebuild would silently lose features.
This snapshot preserves the exact runtime that was in service, so nothing is lost while
the TypeScript sources are back-ported.

## Files here are build artifacts, not sources
- `lib/gate.js` — soft hot-switch reading `/xdata/dsh/robot/vlt-brain/vltctx-state.json` (board-local path)
- `lib/alias.js` — IP/uid alias table `<dataDir>/memory/config/user-alias.json`
- `lib/toolgate.js` — board variant of the tool-face gate (upstream `src/toolgate.ts` is config-driven)
- hand-edits in `lib/{capture,prompt,index,dream,tools,assoc,sidecar}.js`

Do not edit these; port changes into `src/*.ts` and rebuild.
