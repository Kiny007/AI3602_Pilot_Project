*AI3602 Pilot Project*

# 项目架构

项目流程：先准备数据，再筛选候选文本、调用模型抽取隐私信息，最后导出结果，并由 GPT 独立判分评估。运行命令详见 [README](../README.md) 。

本文介绍**文件之间的关系，以及文本在程序中如何被处理。**

## 目录结构

```text
.
├── run.py                   # 命令行入口
├── src/privacy_pilot/       # 数据处理、模型调用、校验与评估
├── prompts/                 # 抽取和复核提示词
│   ├── extract.txt
│   ├── review.txt
│   ├── judge.txt            # GPT 独立判分
│   └── judge_review.txt     # GPT 判分复核
├── config/                  # 数据范围、模型和流程参数
│   ├── default.toml
│   ├── judge.toml           # GPT Responses API 网关配置
│   └── ollama.toml
├── .env.example             # API Key 配置示例
├── data/
│   ├── raw/                 # 下载的原始语料
│   └── prepared/            # 清洗后的输入和划分结果
├── artifacts/               # 运行结果、模型缓存与评估材料
├── tests/                   # 自动化测试
├── docs/                    # 架构、标注规范和验证
└── pyproject.toml           # Python 版本要求和项目配置
```

数据目录和运行目录由命令参数指定。

##  `run.py` 项目入口

根目录的 [run.py](../run.py) 调用 `privacy_pilot.cli.main()`，因此在项目根目录执行 `python run.py ...` 就能运行。`src/privacy_pilot` 中的 [cli.py](../src/privacy_pilot/cli.py) 解析子命令和参数，将任务交给对应模块，并向终端输出结果或错误信息：

| 命令 | 负责的模块 | 作用 |
|---|---|---|
| `download` | `source.py` | 下载和校验语料 |
| `prepare` | `source.py` | 清洗并固定输入 |
| `split` | `evaluation.py` | 将准备好的输入划分为开发集、测试集和收集集 |
| `run` | `pipeline.py` | 执行候选筛选、模型抽取、复核和导出 |
| `judge` | `judge.py` | 对固定样本生成 GPT 参考标签、证据和判分状态 |
| `evaluate` | `evaluation.py` | 比较预测与参考标签，注明参考标签来自 GPT 还是人工 |
| `audit` | `evaluation.py` | 保留可选的人工盲审模板导出功能 |
| `verify`、`compare` | `evaluation.py` | 核验来源与证据，比较两次运行结果 |

`config/default.toml` 保存默认设置，包括输入范围、DeepSeek 模型、调用预算、长文本分块大小和是否复核。`config/ollama.toml` 提供本地模型的配置示例。`config/judge.toml` 单独配置 GPT 判分与复核模型，默认均为 `gpt-6.1-sol`，推理强度为 `high`，请求地址为 `https://api.sjtuagent.com/v1/responses`。

## `src/privacy_pilot` 处理数据

`src/privacy_pilot` 按处理任务拆分模块，主流程由 `pipeline.py` 组织。

### 各文件职责

| 文件 | 主要作用 |
|---|---|
| [cli.py](../src/privacy_pilot/cli.py) | 解析命令行参数，调用对应功能，并输出结果或错误信息。 |
| [config.py](../src/privacy_pilot/config.py) | 读取 TOML 或 JSON 配置，检查数据范围、模型和流程参数是否合法。 |
| [source.py](../src/privacy_pilot/source.py) | 下载和校验语料，读取、清洗、去重原始记录，保存并核验来源信息。 |
| [rules.py](../src/privacy_pilot/rules.py) | 用规则筛选候选文本，规范化用于匹配的文本，并将长文本分块。 |
| [pipeline.py](../src/privacy_pilot/pipeline.py) | 串联筛选、抽取、校验、修复和复核，合并片段结果，记录状态并导出数据。 |
| [model.py](../src/privacy_pilot/model.py) | 调用模型接口，处理重试、调用预算和响应缓存，支持离线重放。 |
| [credentials.py](../src/privacy_pilot/credentials.py) | 从环境变量、`.env` 或终端隐藏输入中读取 API Key。 |
| [schema.py](../src/privacy_pilot/schema.py) | 校验模型输出的字段、标签、主体和原文证据，计算证据位置并合并重复发现。 |
| [judge.py](../src/privacy_pilot/judge.py) | 读取判分配置，固定抽样，组织 GPT 独立判断与复核，保存参考标签和判分来源。 |
| [evaluation.py](../src/privacy_pilot/evaluation.py) | 划分数据集、生成可选盲审模板、比较预测与参考标签，核验结果并比较两次运行。 |
| [common.py](../src/privacy_pilot/common.py) | 提供共用的标签定义、文件读写、哈希计算和异常类型。 |
| [\_\_init\_\_.py](../src/privacy_pilot/__init__.py) | 标识 Python 包并保存项目版本号。 |

### 准备输入

`source.py` 负责下载校验和原始数据读取，支持 JSONL、gzip 和 ZIP 中的逐行记录。准备阶段清理无效记录、去重，并记录数据集版本、文件、ZIP 成员、行号和原始 ID 等来源信息。

准备结果包含正文文件 `input.jsonl`、来源清单 `source_manifest.json` 和排除记录 `excluded.jsonl`。项目使用公开离线语料，**没有实时 Reddit 抓取器**；自行收集的 JSONL 也可通过同一读取流程准备。

### 筛选候选并调用模型

`pipeline.py` 读取准备好的输入，逐条调用 `rules.py` 筛选候选。规则使用**正则表达式和第一人称表达**等线索；未命中的文本记为 `rule_negative`，命中的文本进入模型处理。

`model.py` 负责发送请求、处理有限重试、统计调用预算以及读写响应缓存。API Key 由 `credentials.py` 在第一次实际请求时读取，顺序为环境变量、项目根目录 `.env`、终端隐藏输入。

### 校验证据并复核

模型返回是否存在隐私信息、是否需要进一步审阅，以及具体发现：

- `has_privacy`：是否包含隐私披露。
- `review_required`：是否仍有无法确认的歧义。
- `findings`：每项包含 `tag`、`evidence`、`subject`、`time`、`assertion` 和 `reason`。

[schema.py](../src/privacy_pilot/schema.py) 检查字段、标签和主体约束，并确认每段证据是当前文本片段中唯一出现的连续原文。证据位置 `start`、`end` 由程序计算，使用 Python Unicode 字符索引和左闭右开区间；长文本的片段位置会转换为全文位置。

校验失败时，`pipeline.py` 将错误反馈给模型，在配置允许的次数内要求修复。开启复核后，初次判断为正例或存在歧义的片段会再次交给模型检查。多个片段的发现按标签、位置、主体、时间和断言去重，每个最终标签都有证据，同一条原始文本只计一条结果。

### 导出与评估

通过自动检查的正例写入 `privacy_dataset.jsonl` 和 `privacy_dataset.csv`；所有输入的处理状态写入 `predictions.jsonl`，数量、类别分布和调用统计写入 `summary.json`。

[judge.py](../src/privacy_pilot/judge.py) 对固定样本执行 LLM-as-a-Judge 判分。它只把原文和标签规范交给 GPT，不提供 DeepSeek 的预测标签；随后用配置的复核模型检查 GPT 自己的结果。正例与负例都会进入判分和复核，不经过抽取端的规则过滤。长文本按片段判断，证据映射回全文后合并。

[evaluation.py](../src/privacy_pilot/evaluation.py) 将 DeepSeek 预测与 GPT 参考标签比较。最终正例抽样只计算文本有效率、标签精确率和集合一致率；独立测试集额外计算 Precision、Recall 和 F1。输出明确标记 `reference_kind=llm_judge`，这些是相对于模型参考标签的指标，并非人工金标准确率。原有人工标注评估仍可使用，但同一组评估不能混用两类参考标签。

判分记录保存 `judge_status`、`judge_evidence`、模型与提示词信息；`gold_labels` 作为兼容字段保存参考标签。GPT 不填写 `annotation_seconds` 或 `source_verified`，模型处理时间单独记为 `judge_seconds`。

评分时，`ok` 记录参与指标计算，`needs_review` 记录保留在原始样本中并计为 GPT 弃权。输出中的 `sample_size` 是完整抽样数，`evaluated_count` 是实际评分数，`judge_coverage` 是两者之比；`judge_abstained_count` 和 `judge_abstentions` 分别保存弃权数与记录 ID、原因。例如 50 条中明确判分 47 条时，指标只描述这 47 条，必须同时报告 94% 覆盖率和 3 条弃权。全部弃权时，质量比率返回 `null`。抽取端的弃权另由 `abstained_count` 统计，只统计进入评估的记录。

弃权记录仍需通过文本、来源、证据和预测完整性检查。接口失败、无效证据或未处理请求会阻止评分，不能归入歧义弃权。评分不会改写判分记录或 `summary.json`；只要仍有歧义，判分摘要的 `complete` 就保持 `false`。评估文件中的 `judge_seconds` 累计完整抽样的模型处理时间。

[common.py](../src/privacy_pilot/common.py) 提供各模块共用的标签列表、JSON 读写、哈希计算和异常类型；`__init__.py` 保存项目版本号。

## prompts 约束模型

提示词单独保存在 `prompts` 文件夹。

| 文件 | 用途 |
|---|---|
| [extract.txt](../prompts/extract.txt) | 定义 12 类标签、本人信息与他人信息的边界、原文证据要求和 JSON 输出格式，并提供构造示例 |
| [review.txt](../prompts/review.txt) | 要求模型对照原文检查已有抽取，删除错误发现、修正证据，并补充有明确依据的遗漏 |
| [judge.txt](../prompts/judge.txt) | 要求 GPT 在看不到 DeepSeek 标签的情况下，独立给出参考标签及原文证据 |
| [judge_review.txt](../prompts/judge_review.txt) | 对 GPT 参考标注再做检查，包括负例中的遗漏与无法消除的歧义 |

复核时，程序将 `extract.txt` 与 `review.txt` 拼接，向模型同时提供原文和初次抽取结果，使两次判断使用相同的标签定义。提示词要求把 Reddit 正文当作待分析数据，不能执行正文中的指令，也不能根据姓名、职业等线索推断未明确披露的信息。

GPT 判分使用 `extract.txt` 中相同的标签定义，叠加 `judge.txt`；复核再叠加 `judge_review.txt`。判分不调用搜索工具，也不根据外部信息补全个人事实。

每次运行都会把实际使用的提示词复制到输出目录的 `prompts/` 中。复现时使用这份快照，可避免后续修改提示词影响旧实验。

## 结果、缓存与复现

README 中的 `artifacts/run-v1/` 保存一次运行的结果，`artifacts/cache-v1/` 保存可复用的模型请求和响应。配置快照、来源清单、提示词快照及 `cache_index.json` 则记录这次运行用了哪些输入和缓存。

缓存按接口协议、地址、处理阶段和完整请求内容匹配，请求中包含模型、提示词和推理参数。读取缓存时还会检查响应校验和。使用 `resume` 模式会复用已有响应，缺少时才请求模型；使用 `replay` 模式只读缓存，缺失即报错，不联网，也不读取 API Key。

运行指纹包含项目版本、实际配置、输入哈希、提示词和规则版本。使用同一版本的代码及对应输入、配置、提示词和完整缓存，可以重放处理流程，并通过 `compare` 检查最终 JSONL 是否逐字节一致。耗时和缓存命中次数等运行统计可以不同。

GPT 判分单独使用 `artifacts/judge-final-v1/`、`artifacts/judge-test-v1/` 和 `artifacts/judge-cache-v1/`。`judge --mode replay` 可重放参考标注，耗时字段允许变化；抽取结果的 `compare` 命令不适用于判分目录。

Responses 请求固定 `store=false`，不创建服务端会话；本地响应缓存继续保留。该参数不保证第三方网关不会另行记录请求。网关 Bearer Token 从 `.env` 的 `JUDGE_API_KEY` 读取，额外发送 `x-openai-actor-authorization` 请求头；DeepSeek 继续使用 `DEEPSEEK_API_KEY`。API 格式依据 [OpenAI Responses 文档](https://developers.openai.com/api/reference/resources/responses/methods/create)，第三方网关实际支持情况需以真实连接结果为准。

## 异常记录与测试

如果长文本的任一片段失败，程序不会导出该条文本的部分标签。仍有歧义的记录标为 `needs_review`，保留建议证据供审阅；超过分块上限的记录标为 `skipped_too_long`。这些状态在预测文件中单独记录。

普通记录处理失败后，流程继续处理下一条。鉴权失败或调用预算耗尽时，程序停止发起请求，并为余下记录写入未处理状态，便于之后使用缓存重跑。

[tests/](../tests/) 使用构造文本和模拟模型响应检查数据准备、证据校验、异常处理、GPT 判分、人工参考标签兼容和缓存复现；部分集成测试使用本地 HTTP 服务验证请求行为。
