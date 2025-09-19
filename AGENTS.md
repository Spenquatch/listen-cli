## Agent Guide (read me first)

This repository uses tmux as a wrapper to add hands‑free voice input to any TUI. You are the coding agent. Follow these rules and use `plans.md` for a step‑by‑step build plan.

- Always run our tmux work inside a dedicated server: construct `libtmux.Server(socket_name=…)` so global keybinds never leak into the user’s default tmux server.
- Bind keys with `server.cmd()` and `run-shell -b`. Never use `Session.cmd()` for `bind-key`/`unbind-key` (it auto‑adds `-t`).
- Don’t print in hotkey handlers; it causes pane output. Use HUD (tmux user options) instead.
- Keep paste bracketed: `tmux load-buffer -b …` then `tmux paste-buffer -p -b … -t <pane_id>`.
- HUD lives in tmux user options:
  - `@asr_on` ("1" when REC)
  - `@asr_preview` (partial transcript, truncated)
  - Optional `@asr_message`
- Toggle-off must paste fast: assemble text immediately, paste, then shutdown ASR in background.
- For status styling, match `window-status-style/current-style` to the bar to hide the window list; raise `status-left-length` to fit preview.

Primary reference: `plans.md` — it contains an A→Z linear implementation with all APIs, file paths, commands, and tests. Start there.

