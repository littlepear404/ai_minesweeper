"""AI Minesweeper GUI.

Layout:
  Left  : minesweeper board canvas + status bar + control buttons
  Right : scrollable panel showing the LLM's thinking and actions

The LLM runs in a background thread. The board is updated on the main thread
via a thread-safe command queue. A move delay between turns lets the user
follow what the model is doing.
"""
import ctypes
import json
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from minesweeper import Minesweeper, DIFFICULTIES, NUMBER_COLORS
from llm_client import LLMClient, LLMError

UI_FONT_SIZE = 14
THINK_FONT_SIZE = 14
BOARD_TEXT_FONT = "Consolas"


CONFIG_PATH = "llm_config.json"
STATELESS_MODE = True
ENABLE_BATCH_ACTIONS = True
MAX_BATCH_ACTIONS = 8
ENABLE_REASON = False
PROBABILISTIC_ACTION_BATCH_LIMIT = 1
PREFER_CHORD = True
ACTION_TYPES = {"reveal", "toggle_flag", "chord"}
PROBABILITY_WORDS = (
    "猜", "概率", "不确定", "可能", "也许", "大概", "风险",
    "guess", "probable", "probability", "uncertain", "maybe", "risk",
)

SYSTEM_PROMPT = """你正在玩一个经典的扫雷游戏。棋盘是一个二维网格，其中隐藏着若干地雷。

你的目标:翻开全部不是雷的格子而不踩到任何一颗雷即可获胜。

坐标系:行(row)与列(col)均从 0 开始，左上角为 (0,0)。

棋盘字符含义(每次都将提供当前完整棋盘文本):
  '.'  未翻开格子(内容未知)
  'F'  你插旗标记的格子(你认为可能是雷)
  0-8  已翻开数字格，表示其周围8个相邻格中的雷数
  '*'  地雷(仅在你踩雷输掉、或获胜时才出现)

可用动作(JSON action):
- reveal(row, col) : 翻开一个格子。
    * 若是雷 => 游戏失败(输)。
    * 若是数字格 => 显示周围8格雷数。
    * 若是 0 格 => 自动连锁展开相邻连续安全区域。
- toggle_flag(row, col) : 切换未翻开格子的插旗状态(标记/取消标记你认为是雷的格子)。
- chord(row, col) : 对一个已翻开的数字格执行双击。当该数字格周围已插旗数 == 该数字时，会一次性翻开其周围所有未翻开且未插旗的格子。若你标记的雷有误，双击会踩雷导致失败(与人类双击同样规则)。这能一步推进多个安全格。
    * 若某个数字格周围已插旗数已经等于该数字，且周围仍有未翻开未插旗格，通常应优先考虑 chord，因为它比逐个 reveal 更高效。
    * 但 chord 不是强制动作；如果你怀疑旗标可能来自不确定猜测，或认为单格 reveal 更稳妥，可以选择 reveal。
辅助机制(程序自动处理，你无需重复推理):
- **确定性自动插旗**: 每次翻开新区域后，若某个已翻开数字 N 所在格周围，"未翻开格数 + 已插旗数 == N"，程序会自动为这些未翻开格插旗(它们必然是雷)。结果会直接反映在棋盘文本的 'F' 中，你无需再推理它们，节省你的精力与 token。
- **首点保护**: 你的第一步 reveal 永远不会踩雷(其周围3x3区域无雷)。请放心选择中心附近作为首点。

策略建议(请默默遵循，不必每次复述):
1. 优先翻开数字周围已可推断为安全的格子(已满足雷数约束)。当某个数字格周围的已插旗数 == 该数字，且周围还有未翻开未插旗格时，优先考虑对该数字格使用 chord，以便一次性展开多个确定安全格；若旗标来源不够可靠或局面复杂，也可以改为逐个 reveal。
2. 数字 N 周围若恰好有 N 个未翻开格，且其余邻居已确认安全，则这些未翻开格可能是雷 -> 插旗。
3. 当没有任何确定信息时，未知格应优先翻开周围雷数约束最松的部分；尽量避免从数字较大的区域向外硬猜。
4. 运用数学/逻辑推理;只有纯逻辑无法推进时才允许概率猜测，并简短说明你的胜算理由。

输出要求:
- 每回合必须提交至少一个动作，不能只分析、总结或列计划。
- 可以一次提交多个动作，但一批最多 8 个。
- 批量动作只适用于确定性安全操作，例如合法 chord、确定安全 reveal、确定雷 toggle_flag。
- 若是概率猜测，只能提交 1 个 reveal，不能和其他动作放在同一批。
- 当存在明显安全的 chord 机会时，优先使用 chord 提高效率；但 chord 不是强制动作，不要为了 chord 冒险双击不可靠的旗标。
- 动作优先级建议为：合法 chord > 确定安全 reveal > 确定雷 toggle_flag > 单步概率 reveal。
- 输出必须是严格 JSON，不要输出 markdown，不要输出额外解释。
- reason 可省略；如果输出 reason，只能是极简短语，不要写长篇推理。
- 你看到的"棋盘"文本里坐标以列编号和行编号为准，务必对准。

JSON 格式:
{
  "actions": [
    {"type": "reveal", "row": 0, "col": 0},
    {"type": "toggle_flag", "row": 1, "col": 2},
    {"type": "chord", "row": 3, "col": 4}
  ],
  "reason": "极简理由，可省略"
}
"""


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("AI 扫雷 - LLM 自动玩")
        self.cfg = load_config()
        self._load_runtime_options()
        self.step_no = 0

        self._apply_fonts()

        self.difficulty_var = tk.StringVar(value="beginner")
        self.seed_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="点击\"开始/重启\"开始一局")
        self.thinking_status_var = tk.StringVar(value="空闲")
        self.running = False

        self._build_layout()
        self.game = None
        self.client = None
        self.cmd_queue = queue.Queue()
        self.worker = None

    def _apply_fonts(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=UI_FONT_SIZE)
            except Exception:
                pass

    # -------------------------- UI -------------------------- #
    def _build_layout(self):
        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        ttk.Label(top, text="难度:").pack(side=tk.LEFT)
        for name in DIFFICULTIES:
            ttk.Radiobutton(top, text=name, value=name,
                            variable=self.difficulty_var).pack(side=tk.LEFT)
        ttk.Label(top, text="种子(可空):").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(top, textvariable=self.seed_var, width=8).pack(side=tk.LEFT)

        self.start_btn = ttk.Button(top, text="开始/重启", command=self.on_start)
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.stop_btn = ttk.Button(top, text="停止", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)
        self.continue_btn = ttk.Button(top, text="继续", command=self.on_continue, state=tk.DISABLED)
        self.continue_btn.pack(side=tk.LEFT, padx=8)
        self.edit_btn = ttk.Button(top, text="编辑配置", command=self.on_edit_config)
        self.edit_btn.pack(side=tk.LEFT, padx=8)

        body = ttk.Frame(self.root)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)

        # Left: board
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
        self.canvas = tk.Canvas(left, bg="#bdbdbd", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        status = ttk.Frame(left)
        status.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)

        # Right: thinking panel
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        ttk.Label(right, text="LLM 思考过程").pack(anchor="w")
        self.thinking_status = ttk.Label(right, textvariable=self.thinking_status_var,
                                         font=(None, UI_FONT_SIZE, "bold"))
        self.thinking_status.pack(anchor="w")
        self.thinking = tk.Text(right, wrap=tk.WORD, state=tk.DISABLED,
                                bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                                font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
        self.thinking.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(right, command=self.thinking.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        self.thinking.config(yscrollcommand=sb.set)

    # -------------------------- lifecycle -------------------------- #
    def on_start(self):
        if self.running:
            return
        seed = self.seed_var.get().strip()
        seed = int(seed) if seed else None
        self.game = Minesweeper.from_preset(self.difficulty_var.get(), seed=seed)
        try:
            self.client = LLMClient(self.cfg)
        except LLMError as e:
            messagebox.showerror("配置错误", str(e))
            return
        self.client.reset(SYSTEM_PROMPT)
        self.step_no = 0

        self._clear_thinking()
        self._append_text(f"=== 新游戏开始: {self.difficulty_var.get()} " +
                          f"{self.game.width}x{self.game.height} 雷 {self.game.num_mines} ===\n",
                          tag="sys")
        self._draw_board()
        self._update_status()
        self._begin_worker()

    def on_continue(self):
        if self.running:
            return
        if self.game is None or self.game.state in ("won", "lost", "ready") or self.client is None:
            return
        self._append_text("\n[继续]\n", tag="sys")
        self._begin_worker()

    def _begin_worker(self):
        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except queue.Empty:
                break
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_queue)

    def on_stop(self):
        self.running = False
        self.thinking_status_var.set("空闲")
        self._append_text("\n[已停止]\n", tag="sys")
        # stop permits continue; new game still allowed
        self.stop_btn.config(state=tk.DISABLED)
        if self.game is not None and self.game.state == "playing":
            self.continue_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.NORMAL)

    def on_edit_config(self):
        path = filedialog.askopenfilename(
            initialdir=".", filetypes=[("JSON", "*.json"), ("All", "*.*")],
            title="选择 llm_config.json")
        if not path:
            return
        try:
            self.cfg = load_config(path)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            return
        self._load_runtime_options()
        messagebox.showinfo("已加载", f"配置已加载:\n{path}")

    def _load_runtime_options(self):
        self.cell_size = self.cfg.get("cell_size", getattr(self, "cell_size", 32))
        self.move_delay = self.cfg.get("move_delay", getattr(self, "move_delay", 0.6))
        self.keep_recent = self.cfg.get("keep_recent_turns", getattr(self, "keep_recent", 30))
        self.max_no_action = self.cfg.get(
            "max_no_action_retries", getattr(self, "max_no_action", 3)
        )
        self.stateless_mode = self.cfg.get(
            "STATELESS_MODE", self.cfg.get("stateless_mode", STATELESS_MODE)
        )
        self.enable_batch_actions = self.cfg.get(
            "ENABLE_BATCH_ACTIONS", self.cfg.get("enable_batch_actions", ENABLE_BATCH_ACTIONS)
        )
        self.max_batch_actions = int(self.cfg.get(
            "MAX_BATCH_ACTIONS", self.cfg.get("max_batch_actions", MAX_BATCH_ACTIONS)
        ))
        self.enable_reason = self.cfg.get(
            "ENABLE_REASON", self.cfg.get("enable_reason", ENABLE_REASON)
        )
        self.probabilistic_batch_limit = int(self.cfg.get(
            "PROBABILISTIC_ACTION_BATCH_LIMIT",
            self.cfg.get("probabilistic_action_batch_limit", PROBABILISTIC_ACTION_BATCH_LIMIT),
        ))
        self.prefer_chord = self.cfg.get(
            "PREFER_CHORD", self.cfg.get("prefer_chord", PREFER_CHORD)
        )

    # -------------------------- drawing -------------------------- #
    def _draw_board(self):
        g = self.game
        cs = self.cell_size
        self.canvas.delete("all")
        w = g.width * cs
        h = g.height * cs
        self.canvas.config(width=w, height=h, scrollregion=(0, 0, w, h))
        for r in range(g.height):
            for c in range(g.width):
                x0, y0 = c * cs, r * cs
                x1, y1 = x0 + cs, y0 + cs
                if g.revealed[r][c]:
                    color = "#d0d0d0" if not g.mines[r][c] else "#ff7777"
                    self.canvas.create_rectangle(x0, y0, x1, y1, fill=color,
                                                 outline="#9a9a9a")
                    sym = g.cell_symbol(r, c)
                    if sym == "*":
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2,
                                                text="\U0001F4A3", font=("Arial", int(cs*0.6)))
                    elif sym != "0":
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2, text=sym,
                                                fill=NUMBER_COLORS.get(int(sym), "#000"),
                                                font=("Consolas", int(cs*0.55), "bold"))
                else:
                    self.canvas.create_rectangle(x0, y0, x1, y1, fill="#bdbdbd",
                                                 outline="#9a9a9a")
                    if g.flagged[r][c]:
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2,
                                                text="\U0001F6A9", font=("Arial", int(cs*0.55)))
        if g.explode_cell:
            er, ec = g.explode_cell
            x0, y0 = ec*cs, er*cs
            self.canvas.create_rectangle(x0, y0, x0+cs, y0+cs, outline="red", width=3)

    def _update_status(self):
        g = self.game
        state_text = {"ready": "就绪", "playing": "进行中",
                      "won": "胜利!", "lost": "失败(踩雷)"}[g.state]
        self.status_var.set(
            f"{self.difficulty_var.get()} | {g.width}x{g.height} 雷 {g.num_mines} | "
            f"翻开 {g.revealed_count} | 状态: {state_text}"
        )

    # -------------------------- thinking panel -------------------------- #
    def _clear_thinking(self):
        self.thinking.config(state=tk.NORMAL)
        self.thinking.delete("1.0", tk.END)
        self.thinking.config(state=tk.DISABLED)

    def _append_text(self, text, tag=None):
        self.thinking.config(state=tk.NORMAL)
        if tag:
            self.thinking.insert(tk.END, text, tag)
        else:
            self.thinking.insert(tk.END, text)
        self.thinking.see(tk.END)
        self.thinking.config(state=tk.DISABLED)

    # -------------------------- main thread poller -------------------------- #
    def _poll_queue(self):
        if not self.running and self.cmd_queue.empty():
            return
        try:
            while True:
                kind, payload = self.cmd_queue.get_nowait()
                self._handle_cmd(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_cmd(self, kind, payload):
        if kind == "think_start":
            self.thinking_status_var.set("思考中...")
        elif kind == "think_chunk":
            self._append_text(payload, tag="think")
        elif kind == "think_end":
            self.thinking_status_var.set("空闲")
            if payload:
                self._append_text("\n", tag="think")
        elif kind == "thinking":
            self._append_text(payload + "\n", tag="think")
        elif kind == "action":
            self._append_text(payload + "\n", tag="act")
        elif kind == "result":
            self._append_text(payload + "\n", tag="res")
        elif kind == "error":
            self._append_text(payload + "\n", tag="err")
        elif kind == "redraw":
            self._draw_board()
            self._update_status()
        elif kind == "end":
            self.running = False
            self.thinking_status_var.set("游戏结束")
            if self.client is not None:
                totals = getattr(self.client, "total_usage", {}) or {}
                if totals:
                    self._append_text(
                        "[usage total] "
                        f"requests={totals.get('requests', 0)}, "
                        f"in={totals.get('input_tokens', 0)}, "
                        f"out={totals.get('output_tokens', 0)}, "
                        f"cache={totals.get('cache_tokens', 0)}\n",
                        tag="sys",
                    )
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.continue_btn.config(state=tk.DISABLED)
            self._draw_board()
            self._update_status()

    # -------------------------- LLM worker thread -------------------------- #
    def _run_loop(self):
        no_action = 0
        while self.running:
            g = self.game
            if g.state in ("won", "lost"):
                self._put("end", None)
                return
            self.step_no += 1
            remaining = max(self.max_no_action - no_action, 1)
            snapshot = self._build_state_prompt(no_action, remaining)
            char_count = len(SYSTEM_PROMPT) + len(snapshot)
            self._put("think_start", None)
            try:
                result = self.client.request_json(snapshot)
            except LLMError as e:
                self._put("think_end", True)
                self._put("error", f"[LLM 调用失败] {e}")
                self._put("end", None)
                return
            self._put("think_end", True)
            raw_json = result.get("raw_text", "")
            usage = result.get("usage", {})
            self._put("thinking", self._format_request_log(self.step_no, char_count, usage))
            self._put("thinking", f"[LLM raw] {raw_json[:1000]}")

            actions, parse_errors, reason = self._parse_actions(raw_json)
            if parse_errors:
                for err in parse_errors:
                    self._put("error", f"[JSON 解析/格式错误] {err}")
            if reason and self.enable_reason:
                self._put("thinking", f"[reason] {reason}")
            self._put("thinking", f"[actions] {json.dumps(actions, ensure_ascii=False)}")

            if not actions:
                no_action += 1
                self._put(
                    "result",
                    f"[模型未提交有效动作] 连续无动作 {no_action}/{self.max_no_action}",
                )
                if no_action >= self.max_no_action:
                    self._put("error", "连续多次未执行动作，自动停止。")
                    self._put("end", None)
                    return
                continue
            no_action = 0
            exec_result = self._execute_action_batch(actions, reason)
            self._put(
                "result",
                f"[本轮执行] {exec_result['executed']}/{len(actions)} 步"
                + (f"，提前停止: {exec_result['stop_reason']}" if exec_result["stop_reason"] else ""),
            )
            for skipped in exec_result["skipped"]:
                self._put("result", f"  [跳过] {skipped}")
            if g.state in ("won", "lost"):
                self._put("end", None)
                return

    def _build_state_prompt(self, no_action, remaining):
        g = self.game
        flags = sum(row.count(True) for row in g.flagged)
        unopened = sum(
            1
            for r in range(g.height)
            for c in range(g.width)
            if not g.revealed[r][c]
        )
        reason_line = (
            'reason 可省略；本配置不需要 reason。'
            if not self.enable_reason
            else 'reason 可选且必须极短。'
        )
        return (
            "STATE\n"
            f"rows={g.height}\n"
            f"cols={g.width}\n"
            f"mines_total={g.num_mines}\n"
            f"flags={flags}\n"
            f"unopened={unopened}\n"
            f"revealed_safe={g.revealed_count}\n"
            f"state={g.state}\n"
            f"difficulty={self.difficulty_var.get()}\n"
            f"step={self.step_no}\n"
            f"no_action={no_action}\n"
            f"no_action_limit={self.max_no_action}\n"
            f"remaining_no_action_chances={remaining}\n"
            "board:\n"
            f"{g.to_text()}\n\n"
            "OUTPUT_JSON_ONLY\n"
            f"actions: array, 1..{self.max_batch_actions} items. "
            "type in reveal/toggle_flag/chord. row/col integers. "
            "If probabilistic guess, actions length must be 1 and action must be reveal. "
            f"{reason_line}\n"
        )

    @staticmethod
    def _format_request_log(step_no, char_count, usage):
        token_bits = []
        if usage:
            token_bits.append(f"in={usage.get('input_tokens', 0)}")
            token_bits.append(f"out={usage.get('output_tokens', 0)}")
            token_bits.append(f"cache={usage.get('cache_tokens', 0)}")
        token_text = ", ".join(token_bits) if token_bits else "usage=N/A"
        return f"[step {step_no}] prompt_chars={char_count}, {token_text}"

    def _parse_actions(self, raw_text):
        errors = []
        try:
            payload = json.loads(self._strip_json_markdown(raw_text))
        except json.JSONDecodeError as e:
            return [], [f"不是合法 JSON: {e}"], ""
        if isinstance(payload, list):
            payload = {"actions": payload}
        if not isinstance(payload, dict):
            return [], ["顶层 JSON 必须是 object"], ""

        reason = payload.get("reason", "")
        if reason is not None and not isinstance(reason, str):
            errors.append("reason 必须是字符串")
            reason = ""
        if reason and len(reason) > 80:
            reason = reason[:80]
            errors.append("reason 过长，已截断用于日志")

        raw_actions = payload.get("actions")
        if raw_actions is None and "action" in payload:
            raw_actions = [payload["action"]]
        if raw_actions is None and payload.get("type") in ACTION_TYPES:
            raw_actions = [payload]
        if not isinstance(raw_actions, list):
            return [], errors + ["actions 必须是数组"], reason or ""
        if not raw_actions:
            return [], errors + ["actions 为空"], reason or ""

        limit = self.max_batch_actions if self.enable_batch_actions else 1
        actions = []
        for idx, item in enumerate(raw_actions[:limit]):
            if not isinstance(item, dict):
                errors.append(f"actions[{idx}] 不是 object")
                continue
            action_type = item.get("type")
            row = item.get("row")
            col = item.get("col")
            if action_type not in ACTION_TYPES:
                errors.append(f"actions[{idx}].type 无效: {action_type}")
                continue
            if not isinstance(row, int) or not isinstance(col, int):
                errors.append(f"actions[{idx}].row/col 必须是整数")
                continue
            actions.append({"type": action_type, "row": row, "col": col})
        if len(raw_actions) > limit:
            errors.append(f"动作数超过上限 {limit}，已截断")
        return actions, errors, reason or ""

    @staticmethod
    def _strip_json_markdown(raw_text):
        text = (raw_text or "").strip()
        match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
        return match.group(1).strip() if match else text

    def _execute_action_batch(self, actions, reason):
        skipped = []
        executed = 0
        stop_reason = ""
        original_len = len(actions)
        if self._looks_probabilistic(reason, actions):
            if actions and actions[0]["type"] == "reveal":
                ignored = actions[self.probabilistic_batch_limit:]
                actions = actions[: self.probabilistic_batch_limit]
                skipped.extend(
                    f"{a['type']}({a['row']},{a['col']}): 概率/猜测批次只允许首个 reveal"
                    for a in ignored
                )
            else:
                skipped.extend(
                    f"{a['type']}({a['row']},{a['col']}): 概率/猜测批次首动作不是 reveal"
                    for a in actions
                )
                actions = []

        for idx, action in enumerate(actions):
            if not self.running:
                break
            name = action["type"]
            row = action["row"]
            col = action["col"]
            valid, message = self._validate_action(name, row, col)
            if not valid:
                skipped.append(f"{name}({row},{col}): {message}")
                stop_reason = "动作非法或目标状态已变化"
                break
            self._put("action", f">>> {name}(row={row}, col={col})")
            out, autoflags = self._apply_action(name, row, col)
            executed += 1
            self._put("redraw", None)
            self._put("result", self._format_tool_result(out, name, row, col))
            if autoflags:
                cell_strs = ", ".join(f"({r},{c})" for r, c in autoflags)
                self._put("result", f"  [自动插旗] {len(autoflags)} 格: {cell_strs}")
            result_type = out.get("result")
            if self.game.state in ("won", "lost"):
                stop_reason = "游戏已结束"
                break
            if result_type not in ("safe", "flag", "unflag", "won"):
                stop_reason = out.get("message", "动作无效或无变化")
                break
            if idx < len(actions) - 1 and name in ("reveal", "chord") and len(out.get("cells", [])) > 1:
                stop_reason = "reveal/chord 展开导致局面变化"
                break
            time.sleep(self.move_delay)

        if executed == 0 and original_len and not stop_reason:
            stop_reason = "没有可执行动作"
        return {"executed": executed, "skipped": skipped, "stop_reason": stop_reason}

    @staticmethod
    def _looks_probabilistic(reason, actions):
        text = reason or ""
        for action in actions:
            for value in action.values():
                if isinstance(value, str):
                    text += " " + value
        return any(word.lower() in text.lower() for word in PROBABILITY_WORDS)

    def _validate_action(self, name, row, col):
        g = self.game
        if not g._in_bounds(row, col):
            return False, "坐标越界"
        if g.state in ("won", "lost"):
            return False, "游戏已结束"
        if name == "reveal":
            if g.revealed[row][col]:
                return False, "目标已翻开"
            if g.flagged[row][col]:
                return False, "目标已插旗"
            return True, ""
        if name == "toggle_flag":
            if g.revealed[row][col]:
                return False, "已翻开格不能插旗"
            return True, ""
        if name == "chord":
            if not g.first_move_done:
                return False, "首步前不能 chord"
            if not g.revealed[row][col] or g.mines[row][col]:
                return False, "目标不是已翻开的数字格"
            number = g.numbers[row][col]
            if number == 0:
                return False, "0 格无需 chord"
            flagged_count = sum(1 for nr, nc in g._neighbors(row, col) if g.flagged[nr][nc])
            hidden_unflagged = [
                (nr, nc)
                for nr, nc in g._neighbors(row, col)
                if not g.revealed[nr][nc] and not g.flagged[nr][nc]
            ]
            if flagged_count != number:
                return False, f"周围旗数 {flagged_count} != 数字 {number}"
            if not hidden_unflagged:
                return False, "周围没有未翻开未插旗格"
            return True, ""
        return False, f"未知动作: {name}"

    def _apply_action(self, name, row, col):
        g = self.game
        if name == "reveal":
            out = g.reveal(row, col)
            autoflags = g.auto_flag_certain_mines()
        elif name == "toggle_flag":
            out = g.toggle_flag(row, col)
            autoflags = []
        elif name == "chord":
            out = g.chord(row, col)
            autoflags = g.auto_flag_certain_mines()
        else:
            out = {"result": "invalid", "message": f"未知动作: {name}"}
            autoflags = []
        return out, autoflags

    @staticmethod
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

    @staticmethod
    def _tool_result_to_llm(out, name, row, col, autoflags=None):
        autoflags = autoflags or []
        extra = ""
        if autoflags:
            extra = f" 程序已为 {len(autoflags)} 个确定雷格自动插旗(见快照中的 'F')。"
        r = out.get("result")
        if r == "safe":
            verb = "双击" if name == "chord" else "翻开"
            return (f"{name}({row},{col}) 成功。{verb}后新打开了 "
                    f"{len(out.get('cells',[]))} 个格，棋盘已更新(见下次给你的快照)。"
                    + extra)
        if r == "mine":
            cell = out.get("cell", (row, col))
            return f"{name}({row},{col}) 触发了地雷 {cell}, 你输了这局。"
        if r == "won":
            return f"{name}({row},{col}) 之后你已胜利(翻开全部安全格)。"
        if r == "flag":
            return f"toggle_flag({row},{col}) 已标记为旗。"
        if r == "unflag":
            return f"toggle_flag({row},{col}) 已取消旗。"
        if r == "nochange":
            return f"{name}({row},{col}) 无变化: {out.get('message','')}"
        if r == "invalid":
            return f"{name}({row},{col}) 无效: {out.get('message','')}"
        return str(out)

    def _put(self, kind, payload):
        self.cmd_queue.put((kind, payload))


def main():
    root = tk.Tk()
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        try:
            root.tk.call("tk", "scaling", 1.25)
        except tk.TclError:
            pass
    app = App(root)
    app.thinking.tag_configure("sys", foreground="#56b6c2",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    app.thinking.tag_configure("think", foreground="#d4d4d4",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
    app.thinking.tag_configure("act", foreground="#98c379",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    app.thinking.tag_configure("res", foreground="#61afef",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
    app.thinking.tag_configure("err", foreground="#e06c75",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    root.mainloop()


if __name__ == "__main__":
    main()
