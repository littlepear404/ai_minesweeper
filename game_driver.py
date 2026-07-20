"""Headless AI Minesweeper game driver (no GUI / no tkinter dependency).

This module contains the pure game-loop logic that drives an LLM to play
Minesweeper autonomously. It is deliberately free of any Tkinter / UI import
so it can be exercised from a CLI, a test harness, or the GUI alike.

The single point of integration is the ``emit(kind, payload)`` callback that
the caller supplies. Every board update, thinking fragment, action and game
result is pushed through it, so the GUI and the CLI are just different
``emit`` adapters around the same loop.

Typical CLI usage::

    from game_driver import run_stateless_loop, SYSTEM_PROMPT
    from minesweeper import Minesweeper
    from llm_client import LLMClient

    game = Minesweeper.from_preset("beginner")
    client = LLMClient(load_config())
    run_stateless_loop(game, client, print_emit, system_prompt=SYSTEM_PROMPT)

See ``cli.py`` (``python -m cli``) for a ready-made headless entry point.
"""
import json
import time

from minesweeper import Minesweeper, DIFFICULTIES
from llm_client import LLMClient, LLMError


CONFIG_PATH = "llm_config.json"


SYSTEM_PROMPT = """经典扫雷: 翻开所有非雷格即获胜, 踩雷即输。
坐标 row/col 均从0开始(左上角0,0)。

棋盘为紧凑文本(每行一串, 行从上到下、列从左到右, 索引0):
  '.' 未翻开   'F' 你插的旗(疑雷)   0-8 已翻开格的周围雷数   '*' 雷(仅输/赢时出现)
棋盘末行"已翻开 X/Y 安全格"给出总行/列数, 供你定位 (row,col)。

你是"当前局面动作建议器"(非多步规划器), 不要预判某步之后的新信息。
每轮: 1) 用 ≤2-3 句简体中文极简说明推理要点(哪些格确定安全/是雷/可双击), 勿逐格列棋盘; 2) 紧跟 1~5 个工具调用。

工具(坐标0-index):
- reveal(r,c): 翻开。雷=>输; 数字=>显示周围雷数; 0格=>自动展开相邻安全区。
- toggle_flag(r,c): 切换未翻开格的旗(标记/取消疑雷)。
- chord(r,c): 对已翻开数字格双击; 当周围旗数==该数字时, 一次性翻开周围未翻开未插旗格; 旗标错则踩雷输。

程序会顺序执行你的工具列表, 每步后用最新棋盘重校验; 触发胜利/踩雷/大面积展开或判定为猜测即自动停后续。自动插旗: 数字N周围(未翻开+已插旗)==N 时必为雷, 直接标'F'。首点保护: 第一步永不踩雷(周围3x3无雷), 可选中心附近。

策略: 1) 优先 reveal/chord 确定性安全格, 旗数==数字且有未翻格时必用 chord(最省步骤)。 2) 数字N周围恰N个未翻格即雷->插旗。 3) 无确定格时选雷数约束最松处, 避大数字硬猜。 4) 猜测/单格试探本轮只出这一个动作。 5) 纯逻辑无法推进才概率猜并简述。
上轮若有动作被跳过, 棋盘下方"上轮结果"会告知, 据此调整。"""


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def _format_tool_result(out, name, row, col):
    r = out.get("result")
    if r == "safe":
        n = len(out.get("cells", []))
        if name == "chord":
            return f"  -> 双击 ({row},{col}) 成功, 展开 {n} 格"
        return f"  -> 翻开 ({row},{col}) 成功, 展开 {n} 格"
    if r == "mine":
        if name == "chord":
            cell = out.get("cell", (row, col))
            return f"  -> 双击踩雷! {cell} 游戏失败"
        return f"  -> 踩雷! ({row},{col}) 游戏失败"
    if r == "won":
        return "  -> 胜利! 已翻开全部安全格"
    if r == "flag":
        return f"  -> 已在 ({row},{col}) 插旗"
    if r == "unflag":
        return f"  -> 已取消 ({row},{col}) 的旗"
    if r == "nochange":
        return f"  -> 无变化: {out.get('message','')}"
    if r == "invalid":
        return f"  -> 无效: {out.get('message','')}"
    if r == "over":
        return f"  -> 游戏已结束: {out.get('message','')}"
    return f"  -> {out}"


def run_stateless_loop(game, client, emit, *, move_delay=0.6,
                       max_no_action=10, stop_check=None,
                       system_prompt=SYSTEM_PROMPT):
    """Drive the LLM through a full stateless game of Minesweeper.

    The loop sends ``SYSTEM_PROMPT`` + the current compact board snapshot on
    every model call (no history is kept -- that is what "stateless" means),
    then executes each returned tool call against ``game`` in order,
    re-validating against the live board between steps.

    Parameters
    ----------
    game : Minesweeper
        The game instance to play. Its state machine is mutated in place.
    client : LLMClient
        An initialised LLM client exposing ``call_stateless_stream``.
    emit : Callable[[str, object], None]
        Callback receiving ``(kind, payload)`` tuples. The GUI pushes these
        onto its thread-safe command queue; a CLI adapter just prints them.
        Recognised ``kind`` values: ``think_start``, ``think_chunk``,
        ``think_end``, ``thinking``, ``action``, ``result``, ``error``,
        ``redraw``, ``end``.
    move_delay : float
        Seconds to sleep between executed actions.
    max_no_action : int
        Stop after this many consecutive empty / all-skipped rounds.
    stop_check : Callable[[], bool] or None
        Called before each step; when it returns True the loop aborts
        cleanly (used for a "stop" button or a threading.Event). Defaults to
        a never-stop lambda.
    system_prompt : str
        The prompt handed to the model each turn. Kept in sync with the
        board snapshot format produced by ``Minesweeper.to_text_compact``.
    """
    if stop_check is None:
        stop_check = lambda: False
    move_count = 0
    no_action = 0
    no_progress = 0  # rounds where the model returned calls but none executed
    last_summary = ""

    def _end(result):
        # Single exit signal: always carries the outcome and action count so
        # callers (GUI run history, CLI batch stats) never have to guess.
        emit("end", {"result": result, "moves": move_count})

    while True:
        if stop_check():
            _end("stopped")
            return
        g = game
        if g.state in ("won", "lost"):
            _end(g.state)
            return
        hint = ""
        if not g.first_move_done:
            hint = "\n(第一步: 首点周围永远安全, 推荐中心附近)"
        snapshot = (
            "棋盘(每行一串, 行从上到下, 列从左到右, 索引0开始; "
            "'.' 未翻开, 'F' 旗, '0'-'8' 已翻开雷数):\n"
            + g.to_text_compact() + "\n" + g.summary()
            + hint + last_summary
        )
        if no_action > 0:
            snapshot += f"\n(上一轮未返回工具调用，连续空轮 {no_action} 次)"
        emit("think_start", None)
        thinking = ""
        tool_calls = []
        try:
            for kind, val in client.call_stateless_stream(system_prompt, snapshot):
                if stop_check():
                    emit("think_end", None)
                    _end("stopped")
                    return
                if kind == "chunk":
                    emit("think_chunk", val)
                    thinking += val
                elif kind == "final":
                    tool_calls = val.get("tool_calls") or []
        except LLMError as e:
            emit("think_end", True)
            emit("error", f"[LLM 调用失败] {e}")
            _end("error")
            return
        emit("think_end", True)
        emit("think_chunk", "\n")

        if not tool_calls:
            no_action += 1
            emit("result", f"[模型未返回工具调用] 连续空轮 {no_action}")
            if no_action >= max_no_action:
                emit("error", "连续多次空轮，自动停止。")
                _end("stopped")
                return
            continue
        no_action = 0

        # ------ batch execution ------
        action_log, skip_log = [], []
        progressed = False  # did any call change the board this round?
        for i, tc in enumerate(tool_calls):
            if stop_check():
                _end("stopped")
                return
            name = tc.get("name")
            args = tc.get("args") or {}
            row, col = args.get("row"), args.get("col")
            if row is None or col is None:
                skip_log.append(f"{name}(?,?)原因:缺少参数")
                emit("result", f"[跳过] {name} 缺少参数")
                continue
            emit("action", f">>> {name}(row={row}, col={col})")
            if name == "reveal":
                out = g.reveal(row, col)
            elif name == "toggle_flag":
                out = g.toggle_flag(row, col)
            elif name == "chord":
                out = g.chord(row, col)
            else:
                skip_log.append(f"{name}({row},{col})原因:未知工具")
                emit("result", f"[跳过] 未知工具: {name}")
                break
            emit("redraw", None)
            move_count += 1
            res = out.get("result")
            emit("result", _format_tool_result(out, name, row, col))
            autoflags = g.auto_flag_certain_mines() if name in ("reveal", "chord") else []
            if autoflags:
                cells = ", ".join(f"({r},{c})" for r, c in autoflags)
                emit("result", f"  [自动插旗] {len(autoflags)} 格: {cells}")

            if res in ("invalid", "over"):
                skip_log.append(f"{name}({row},{col})原因:{out.get('message','')}")
                emit("result", "[批量中断: 动作无效/游戏已结束]")
                break

            # Only a call that actually changed the board counts as progress.
            if res in ("safe", "mine", "won", "flag", "unflag"):
                progressed = True

            action_log.append(f"{name}({row},{col})")

            if res == "mine":
                break
            if res == "won":
                action_log.append("胜利!")
                break
            if res == "nochange":
                emit("result", f"[批量中断: {name} 无变化]")
                break

            # heuristic: single-cell reveal = no structural progress (规则 7)
            if name == "reveal":
                cells_opened = len(out.get("cells", []))
                if cells_opened <= 1:
                    emit("result", "[批量中断: 单格翻开, 后续动作可能基于旧信息]")
                    break

            time.sleep(move_delay)

        # round summary for next LLM call
        # A model that keeps returning calls which never change the board
        # (e.g. re-calling an already-revealed cell -> "nochange") must not
        # spin forever. Count those no-progress rounds separately from
        # no_action (which only covers empty tool-call rounds) and stop.
        if tool_calls and not progressed:
            no_progress += 1
        else:
            no_progress = 0
        if no_progress >= max_no_action:
            emit("error", "连续多次返回无效/无执行动作，自动停止。")
            _end("stopped")
            return
        parts = []
        if action_log:
            parts.append("上轮已执行: " + ", ".join(action_log))
        if skip_log:
            parts.append("上轮跳过: " + ", ".join(skip_log))
        last_summary = ("\n\n" + "\n".join(parts)) if parts else ""

        if g.state in ("won", "lost"):
            _end(g.state)
            return
