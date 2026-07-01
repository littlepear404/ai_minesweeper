# AGENTS.md

Repository: AI Minesweeper — a Tkinter desktop app where an LLM (OpenAI- or
Anthropic-compatible API) plays classic Minesweeper autonomously via tool calls.

## Run

```bash
pip install -r requirements.txt        # only `requests` beyond stdlib
python main.py                          # launches the Tkinter GUI
```

No build step, no tests yet. Verify changes with `python -m py_compile
minesweeper.py llm_client.py gui.py main.py` and the smoke check below.

## Architecture

- `minesweeper.py` — pure game logic. No I/O, no Tkinter. First click is
  always safe (3x3 safe zone generated on first reveal via `_place_mines`).
  Board state machine: `ready -> playing -> won|lost`. Use `Minesweeper.from_preset(name)`; presets live in `DIFFICULTIES` (beginner/intermediate/expert).
- `gui.py` — Tkinter UI + the LLM driver thread. **Two threads**: the Tk main
  loop owns the board, the LLM worker (`_run_loop`) runs the model loop. They
  never touch shared widgets directly — the worker posts `(kind, payload)`
  tuples to `self.cmd_queue` and the main thread polls it every 100ms via
  `_poll_queue`/`_handle_cmd`. Keep this boundary: worker = game + client only,
  all widget writes go through the queue.
- `llm_client.py` — thin HTTP client. Supports `provider: openai` (OpenAI
  chat-completions, also handles `reasoning_content` from reasoning models) and
  `provider: anthropic` (Anthropic `/messages` with `tool_use`/`tool_result`
  blocks). History is kept in each provider's native format inside the client;
  `trim_history` keeps the system/first message and a sliding window of recent
  turns. Tools are `reveal` and `toggle_flag` only (defined in `COMMON_TOOLS`).
- `main.py` — entrypoint, just import-guards Tkinter/requests then calls `gui.main()`.
- `llm_config.json` — **the only** API config (key, base_url, model, tempo).
  Tracked in git with a placeholder key; real keys are secrets and must never
  be committed. `keep_recent_turns`, `move_delay`, `max_no_action_retries`
  tune the game loop.

## Conventions that matter

- Threading: do not call Tk widgets from `_run_loop`. Push a queue command.
  `time.sleep(self.move_delay)` between actions is intentional so the user can
  follow the game.
- Tools: the LLM gets exactly two functions — `reveal(row, col)` and
  `toggle_flag(row, col)`. Coordinates are **0-indexed**; the system prompt
  states this and the board text snapshot labels rows/cols. Don't add 1.
- Board text (`Minesweeper.to_text`) is the LLM's whole view of the game; it
  hides unrevealed cells as `.` and flags as `F`. Every turn sends a fresh
  snapshot + a user message (see `_run_loop`). If you change the snapshot
  format, also update `SYSTEM_PROMPT` in `gui.py`.
- First-move safety is implemented lazily — mines are placed *after* the first
  `reveal`, excluding a 3x3 area around the clicked cell. Don't call
  `_place_mines` directly.
- Windows is the dev platform; the console may mojibake Chinese from stdlib
  print output — that's a console codepage artifact, not a file bug. Files are
  UTF-8.
- Stopping mid-game: `on_stop` flips `self.running=False`; the worker checks
  this between actions. Don't block the main thread on the worker.

## Smoke check (no API key needed)

```bash
python -c "from minesweeper import Minesweeper; g=Minesweeper.from_preset('beginner'); r=g.reveal(0,0); print(r['result'], r['cells'][:3], g.state)"
python -c "from llm_client import build_openai_tools, build_anthropic_tools; print(len(build_openai_tools()), len(build_anthropic_tools()))"
```

First should print `safe ... playing`; both tool counts should be `2`.

## Config fields (llm_config.json)

`provider` (`openai`|`anthropic`), `api_base_url`, `api_key`, `model`,
`temperature`, `max_tokens`, `request_timeout`, `move_delay` (seconds between
LLM actions), `keep_recent_turns` (history window, 0 = keep all),
`max_no_action_retries` (stop after N consecutive no-call turns). `cell_size`
(optional, px) also lives here.

## Known limits / next steps

- No persistent scoring or run history.
- No tests; candidate targets: `minesweeper.py` logic (win/lose/flood/flag),
  `llm_client.py` tool-result formatting (mock `requests.post`).
- LLM debugging requires a real API key in `llm_config.json` — deferred until
  the user provides one. With the placeholder key, `LLMClient.__init__` raises
  `LLMError`, so "开始/重启" shows a config-error dialog (expected).