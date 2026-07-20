# AI Minesweeper

AI Minesweeper 是一个桌面版扫雷程序，由大语言模型通过工具调用自动玩扫雷。程序支持 OpenAI 兼容接口和 Anthropic Messages 接口。

## 环境安装

需要 Python 3.9 或更新版本。

建议先创建虚拟环境：

```bash
python -m venv .venv
```

激活虚拟环境：

```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

目前额外依赖只有 `requests`，GUI 使用 Python 标准库里的 Tkinter。

## 配置 API

程序启动时会读取项目根目录下的 `llm_config.json`。可以从模板复制一份：

```bash
cp llm_config.template.json llm_config.json
```

如果你的环境没有 `cp` 命令，也可以手动复制 `llm_config.template.json` 并改名为 `llm_config.json`。

### OpenAI 兼容接口

适用于 OpenAI、DeepSeek、以及其他兼容 `/chat/completions` 的服务。

```json
{
  "provider": "openai",
  "api_base_url": "https://api.openai.com/v1",
  "api_key": "YOUR_API_KEY_HERE",
  "model": "gpt-4.1-mini",
  "temperature": 0.4,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "solver_mode": "assist",
  "cell_size": 32
}
```

DeepSeek 示例：

```json
{
  "provider": "openai",
  "api_base_url": "https://api.deepseek.com",
  "api_key": "YOUR_API_KEY_HERE",
  "model": "deepseek-chat",
  "temperature": 0.4,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "solver_mode": "assist",
  "cell_size": 32
}
```

### Anthropic 接口

适用于 Anthropic `/messages` 接口。

```json
{
  "provider": "anthropic",
  "api_base_url": "https://api.anthropic.com/v1",
  "api_key": "YOUR_API_KEY_HERE",
  "model": "claude-3-5-sonnet-latest",
  "temperature": 0.4,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "solver_mode": "assist",
  "cell_size": 32
}
```

## 配置项说明

- `provider`: 接口类型，填写 `openai` 或 `anthropic`。
- `api_base_url`: API 基础地址。OpenAI 兼容接口通常以 `/v1` 结尾，也可以填写兼容服务商提供的地址。
- `api_key`: API 密钥。请替换 `YOUR_API_KEY_HERE`，不要提交真实密钥。
- `model`: 要使用的模型名称。
- `temperature`: 模型输出随机性，建议保持较低。
- `max_tokens`: 单次模型回复的最大 token 数。
- `request_timeout`: API 请求超时时间，单位为秒。
- `move_delay`: 每一步动作后的等待时间，单位为秒，方便观察 AI 操作。
- `max_no_action_retries`: 连续没有工具调用多少次后停止本局。
- `solver_mode`: `assist`（默认）表示先由本地确定性求解器免费完成所有可推导的步骤，只在需要猜测时才调用 LLM，显著降低 token 消耗；`off` 表示每一步都交给 LLM。
- `reasoning_effort`: 可选，如 `"low"`，仅对支持该参数的 OpenAI 推理模型透传，用于压缩思考 token。
- `cell_size`: 棋盘格子大小，单位为像素。

## 运行程序

配置好 `llm_config.json` 后运行：

```bash
python main.py
```

启动后点击“开始/重启”，模型会根据当前棋盘自动调用工具执行扫雷动作。

## 无头模式与批量评测

不启动 GUI，直接在终端运行同一套游戏循环：

```bash
python -m cli -d beginner --seed 123
```

批量评测（静默连跑 N 局，逐局写入 `run_history.jsonl`，最后输出胜率与 token 汇总）：

```bash
python -m cli -d intermediate --games 20 --seed-start 1000
```

## 注意事项

- `llm_config.json` 可能包含真实 API key，请不要公开或提交到公共仓库。
- 如果使用 OpenAI 兼容服务，`provider` 仍然填写 `openai`。
- 如果 API 地址已经包含 `/chat/completions` 或 `/messages`，程序会自动归一化处理。
