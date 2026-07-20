"""Headless CLI driver for the AI Minesweeper.

Lets you run the LLM game loop without any Tkinter GUI -- ideal for
debugging the model's behaviour, token usage, or tool-call parsing from a
terminal. The driver itself never imports tkinter; it just adapts the
``emit`` callback to plain ``print`` calls.

Usage::

    python -m cli --difficulty beginner --seed 123
    python -m cli --config my_config.json --move-delay 0.2
    python -m cli -d intermediate --games 20 --seed-start 1000   # batch eval

Flags:
    -d, --difficulty  beginner | intermediate | expert  (default: beginner)
    -s, --seed        optional integer seed for a reproducible single game
    -c, --config      path to llm_config.json (default: llm_config.json)
    --move-delay      seconds between actions (overrides config)
    --max-no-action   stop after N consecutive empty / skipped rounds
    --games           number of games to play back-to-back (default: 1);
                      >1 enables quiet batch mode and records every game
                      to run_history.jsonl
    --seed-start      batch mode: first game's seed (later games increment)
"""
import argparse
import sys
import threading
import time

from minesweeper import Minesweeper, DIFFICULTIES
from llm_client import LLMClient, LLMError
from run_history import record as record_run
from game_driver import run_stateless_loop, SYSTEM_PROMPT, load_config


def _make_emit(verbose=True, sink=None):
    """Build an emit adapter.

    verbose=True prints the full thinking stream; verbose=False (batch mode)
    stays quiet. The final ("end", payload) event is always stored into
    ``sink`` (a one-element list) so the caller can read game stats.
    """
    def _emit(kind, payload):
        if kind == "end":
            if sink is not None:
                sink.append(payload or {})
            if verbose:
                payload = payload or {}
                print(f"\n=== 游戏结束: {payload.get('result', '?')} | "
                      f"动作数 {payload.get('moves', 0)} | "
                      f"tokens 输入 {payload.get('input_tokens', 0)} / "
                      f"输出 {payload.get('output_tokens', 0)} ===", flush=True)
            return
        if not verbose:
            return
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
        elif kind == "usage":
            payload = payload or {}
            print(f"  [tokens] 输入 {payload.get('input_tokens')} / "
                  f"输出 {payload.get('output_tokens')}", flush=True)
        elif kind == "error":
            print(f"[错误] {payload}", flush=True)
        elif kind == "redraw":
            pass  # headless: nothing to redraw
    return _emit


def _print_board(game):
    print("\n当前棋盘:")
    for line in game.to_text_compact().split("\n"):
        print("  " + line)
    print("  " + game.summary())


def _install_sigint(stop_event):
    def _on_sigint(*_):
        stop_event.set()
    try:
        import signal
        signal.signal(signal.SIGINT, _on_sigint)
    except (ImportError, ValueError):
        pass


def _play_one(game, client, cfg, stop_event, verbose):
    """Run a single game; returns the final end payload dict."""
    end_sink = []
    run_stateless_loop(
        game, client, _make_emit(verbose=verbose, sink=end_sink),
        move_delay=cfg.get("move_delay", 0.6),
        max_no_action=cfg.get("max_no_action_retries", 10),
        stop_check=stop_event.is_set,
        system_prompt=SYSTEM_PROMPT,
        solver_mode=cfg.get("solver_mode", "assist"),
    )
    return end_sink[0] if end_sink else {"result": "stopped", "moves": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Headless AI Minesweeper driver")
    parser.add_argument("-d", "--difficulty", choices=list(DIFFICULTIES),
                        default="beginner")
    parser.add_argument("-s", "--seed", type=int, default=None)
    parser.add_argument("-c", "--config", default="llm_config.json")
    parser.add_argument("--move-delay", type=float, default=None)
    parser.add_argument("--max-no-action", type=int, default=None)
    parser.add_argument("--solver", choices=["off", "assist"], default=None,
                        help="local deterministic solver mode (default: config)")
    parser.add_argument("--games", type=int, default=1,
                        help="batch mode: number of games to play")
    parser.add_argument("--seed-start", type=int, default=None,
                        help="batch mode: seed of the first game (increments)")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.move_delay is not None:
        cfg["move_delay"] = args.move_delay
    if args.max_no_action is not None:
        cfg["max_no_action_retries"] = args.max_no_action
    if args.solver is not None:
        cfg["solver_mode"] = args.solver

    try:
        client = LLMClient(cfg)
    except LLMError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        print("请先在 llm_config.json 中填入有效的 api_key / api_base_url。",
              file=sys.stderr)
        return 2

    stop_event = threading.Event()
    _install_sigint(stop_event)

    # ---------------- batch mode ----------------
    if args.games > 1:
        print(f"=== 批量评测: {args.games} 局 {args.difficulty} | "
              f"模型 {client.model} ({client.provider}) | "
              f"solver={cfg.get('solver_mode', 'assist')} ===")
        results = []
        for i in range(args.games):
            if stop_event.is_set():
                print("[已中断] 提前结束批量评测。")
                break
            seed = (args.seed_start + i) if args.seed_start is not None else None
            game = Minesweeper.from_preset(args.difficulty, seed=seed)
            t0 = time.time()
            end = _play_one(game, client, cfg, stop_event, verbose=False)
            duration = time.time() - t0
            result = end.get("result", "?")
            record_run(
                result,
                difficulty=args.difficulty,
                width=game.width, height=game.height,
                num_mines=game.num_mines,
                model=client.model, provider=client.provider,
                moves=end.get("moves", 0),
                revealed=game.revealed_count,
                duration_s=duration,
                seed=seed,
                input_tokens=end.get("input_tokens"),
                output_tokens=end.get("output_tokens"),
            )
            results.append((result, end))
            print(f"  [{i + 1}/{args.games}] {result:<8} "
                  f"动作 {end.get('moves', 0):<4} "
                  f"tokens {end.get('input_tokens', 0)}/"
                  f"{end.get('output_tokens', 0)} "
                  f"seed={seed}", flush=True)
        n = len(results)
        if n:
            won = sum(1 for r, _ in results if r == "won")
            tot_in = sum(e.get("input_tokens", 0) for _, e in results)
            tot_out = sum(e.get("output_tokens", 0) for _, e in results)
            avg_moves = sum(e.get("moves", 0) for _, e in results) / n
            print(f"\n=== 汇总: {n} 局 | 胜 {won} ({won * 100 // n}%) | "
                  f"平均动作 {avg_moves:.1f} | "
                  f"总 tokens {tot_in}/{tot_out} | "
                  f"平均每局 {tot_in // n}/{tot_out // n} ===")
        return 0

    # ---------------- single game ----------------
    game = Minesweeper.from_preset(args.difficulty, seed=args.seed)
    print(f"=== 开始一局: {args.difficulty} "
          f"{game.width}x{game.height} 雷 {game.num_mines} (无状态模式) ===")
    _print_board(game)

    _play_one(game, client, cfg, stop_event, verbose=True)

    _print_board(game)
    print(f"最终状态: {game.state} | 翻开 {game.revealed_count}/"
          f"{game.width * game.height - game.num_mines}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
