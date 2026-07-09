"""Headless CLI driver for the AI Minesweeper.

Lets you run the LLM game loop without any Tkinter GUI -- ideal for
debugging the model's behaviour, token usage, or tool-call parsing from a
terminal. The driver itself never imports tkinter; it just adapts the
``emit`` callback to plain ``print`` calls.

Usage::

    python -m cli --difficulty beginner --seed 123
    python -m cli --config my_config.json --move-delay 0.2

Flags:
    -d, --difficulty  beginner | intermediate | expert  (default: beginner)
    -s, --seed        optional integer seed for a reproducible board
    -c, --config      path to llm_config.json (default: llm_config.json)
    --move-delay      seconds between actions (overrides config)
    --max-no-action   stop after N consecutive empty / skipped rounds
"""
import argparse
import sys
import threading
import time

from minesweeper import Minesweeper, DIFFICULTIES
from llm_client import LLMClient, LLMError
from game_driver import run_stateless_loop, SYSTEM_PROMPT, load_config


def _emit(kind, payload):
    """Map the driver's (kind, payload) tuples onto terminal output."""
    if kind == "think_start":
        print("\n--- 思考中 ---", flush=True)
    elif kind == "think_chunk":
        print(payload, end="", flush=True)
    elif kind == "think_end":
        print("", flush=True)
    elif kind == "action":
        print(f"  {payload}", flush=True)
    elif kind == "result":
        print(f"  {payload}", flush=True)
    elif kind == "error":
        print(f"[错误] {payload}", flush=True)
    elif kind == "redraw":
        pass  # headless: nothing to redraw
    elif kind == "end":
        print("\n=== 游戏结束 ===", flush=True)


def _print_board(game):
    print("\n当前棋盘:")
    for line in game.to_text_compact().split("\n"):
        print("  " + line)
    print("  " + game.summary())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Headless AI Minesweeper driver")
    parser.add_argument("-d", "--difficulty", choices=list(DIFFICULTIES),
                        default="beginner")
    parser.add_argument("-s", "--seed", type=int, default=None)
    parser.add_argument("-c", "--config", default="llm_config.json")
    parser.add_argument("--move-delay", type=float, default=None)
    parser.add_argument("--max-no-action", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.move_delay is not None:
        cfg["move_delay"] = args.move_delay
    if args.max_no_action is not None:
        cfg["max_no_action_retries"] = args.max_no_action

    game = Minesweeper.from_preset(args.difficulty, seed=args.seed)
    try:
        client = LLMClient(cfg)
    except LLMError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        print("请先在 llm_config.json 中填入有效的 api_key / api_base_url。",
              file=sys.stderr)
        return 2

    print(f"=== 开始一局: {args.difficulty} "
          f"{game.width}x{game.height} 雷 {game.num_mines} (无状态模式) ===")
    _print_board(game)

    stop_event = threading.Event()

    def stop_check():
        return stop_event.is_set()

    # run the loop on the main thread (no GUI worker thread needed here);
    # expose Ctrl-C as a clean stop.
    def _on_sigint(*_):
        stop_event.set()
    try:
        import signal
        signal.signal(signal.SIGINT, _on_sigint)
    except (ImportError, ValueError):
        pass

    run_stateless_loop(
        game, client, _emit,
        move_delay=cfg.get("move_delay", 0.6),
        max_no_action=cfg.get("max_no_action_retries", 10),
        stop_check=stop_check,
        system_prompt=SYSTEM_PROMPT,
    )

    _print_board(game)
    print(f"最终状态: {game.state} | 翻开 {game.revealed_count}/"
          f"{game.width * game.height - game.num_mines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
