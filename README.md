# AI Minesweeper

AI Minesweeper 是一个桌面版扫雷程序，由大语言模型通过工具调用自动玩扫雷。程序支持 OpenAI 兼容接口和 Anthropic Messages 接口，并支持一轮返回多个工具调用后按顺序执行。

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
  "tool_temperature": 0,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "STATELESS_MODE": true,
  "ENABLE_BATCH_ACTIONS": true,
  "MAX_BATCH_ACTIONS": 8,
  "ENABLE_REASON": false,
  "PROBABILISTIC_ACTION_BATCH_LIMIT": 1,
  "PREFER_CHORD": true,
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
  "tool_temperature": 0,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "STATELESS_MODE": true,
  "ENABLE_BATCH_ACTIONS": true,
  "MAX_BATCH_ACTIONS": 8,
  "ENABLE_REASON": false,
  "PROBABILISTIC_ACTION_BATCH_LIMIT": 1,
  "PREFER_CHORD": true,
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
  "tool_temperature": 0,
  "max_tokens": 1024,
  "request_timeout": 120,
  "move_delay": 0.6,
  "max_no_action_retries": 10,
  "STATELESS_MODE": true,
  "ENABLE_BATCH_ACTIONS": true,
  "MAX_BATCH_ACTIONS": 8,
  "ENABLE_REASON": false,
  "PROBABILISTIC_ACTION_BATCH_LIMIT": 1,
  "PREFER_CHORD": true,
  "cell_size": 32
}
```

## 配置项说明

- `provider`: 接口类型，填写 `openai` 或 `anthropic`。
- `api_base_url`: API 基础地址。OpenAI 兼容接口通常以 `/v1` 结尾，也可以填写兼容服务商提供的地址。
- `api_key`: API 密钥。请替换 `YOUR_API_KEY_HERE`，不要提交真实密钥。
- `model`: 要使用的模型名称。
- `temperature`: 模型输出随机性，建议保持较低。
- `tool_temperature`: 工具调用模式的随机性，默认 `0`，优先保证动作稳定。
- `max_tokens`: 单次模型回复的最大 token 数。
- `request_timeout`: API 请求超时时间，单位为秒。
- `move_delay`: 每一步动作后的等待时间，单位为秒，方便观察 AI 操作。
- `max_no_action_retries`: 连续没有工具调用多少次后停止本局。
- `STATELESS_MODE`: 默认 `true`。每次请求只发送固定 system prompt 和当前棋盘状态，不携带旧 assistant、旧动作、旧工具结果或旧棋盘快照。
- `ENABLE_BATCH_ACTIONS`: 默认 `true`。模型可在一次回复中提交多个确定性工具调用。
- `MAX_BATCH_ACTIONS`: 单批动作上限，默认 `8`。
- `ENABLE_REASON`: 默认 `false`。关闭后提示模型省略 reason，减少输出 token。
- `PROBABILISTIC_ACTION_BATCH_LIMIT`: 概率猜测批次上限，默认 `1`。
- `PREFER_CHORD`: 默认 `true`。提示模型优先考虑安全 chord，但程序不会强制 chord。
- `cell_size`: 棋盘格子大小，单位为像素。

## 批量工具调用协议

每回合模型必须至少调用一个工具；可以一次返回多个工具调用，程序会按模型返回顺序逐个执行：

```text
reveal(row=0, col=0)
toggle_flag(row=1, col=2)
chord(row=3, col=4)
```

可用工具只有 `reveal`、`toggle_flag`、`chord`；`row` 和 `col` 必须是 0-indexed 整数。程序会按顺序执行工具调用，每一步后立即更新棋盘。如果 reveal/chord 展开了新区域、动作非法、目标状态已变化、或游戏胜负已定，本批后续动作会停止执行。概率猜测只允许执行首个 `reveal`。

LLM 请求仍是无状态的：每次只发送固定 system prompt、当前棋盘状态、局面统计信息和工具定义，不携带旧 assistant 内容、旧工具结果或旧棋盘快照。

## 运行程序

配置好 `llm_config.json` 后运行：

```bash
python main.py
```

启动后点击“开始/重启”，模型会根据当前棋盘自动调用工具执行扫雷动作。

## 注意事项

- `llm_config.json` 可能包含真实 API key，请不要公开或提交到公共仓库。
- 如果使用 OpenAI 兼容服务，`provider` 仍然填写 `openai`。
- 如果 API 地址已经包含 `/chat/completions` 或 `/messages`，程序会自动归一化处理。
