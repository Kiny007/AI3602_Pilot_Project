*AI3602 Pilot Project*

# 项目架构

项目流程：先准备数据，再筛选候选文本、调用模型抽取隐私信息，最后导出结果并评估。运行命令详见 [README](../README.md) 。

本文介绍**文件之间的关系，以及文本在程序中如何被处理。**

## 目录结构

```text
.
├── run.py                   # 命令行入口
├── src/privacy_pilot/       # 数据处理、模型调用、校验与评估
├── prompts/                 # 抽取和复核提示词
│   ├── extract.txt
│   └── review.txt
├── config/                  # 数据范围、模型和流程参数
│   ├── default.toml
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

数据目录和运行目录由命令参数指定，图中使用 README 的路径约定。首次运行时，程序会创建相应的输出目录。

##  `run.py` 项目入口

根目录的 [run.py](../run.py) 调用 `privacy_pilot.cli.main()`。因此，在项目根目录执行 `python run.py ...` 就能运行。

[cli.py](../src/privacy_pilot/cli.py) 解析子命令和参数，将任务交给对应模块，并向终端输出结果或错误信息：

| 命令 | 负责的模块 | 完成的工作 |
|---|---|---|
| `download`、`prepare` | `source.py` | 下载和校验语料，清洗并固定本次输入 |
| `split` | `evaluation.py` | 将准备好的输入划分为开发集、测试集和收集集 |
| `run` | `pipeline.py` | 执行候选筛选、模型抽取、复核和导出 |
| `audit`、`evaluate` | `evaluation.py` | 生成盲审模板，根据人工标注计算指标 |
| `verify`、`compare` | `evaluation.py` | 核验来源与证据，比较两次运行结果 |

[config/default.toml](../config/default.toml) 保存默认设置，包括输入范围、DeepSeek 模型、调用预算、长文本分块大小和是否复核。[config.py](../src/privacy_pilot/config.py) 读取配置并检查取值；运行时也可以通过 `--config` 指定其他 TOML 或 JSON 文件。`config/ollama.toml` 提供本地模型的配置示例。

## src/privacy_pilot 如何处理数据

[src/privacy_pilot/](../src/privacy_pilot/) 按处理任务拆分模块，主流程由 `pipeline.py` 组织。

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
| [evaluation.py](../src/privacy_pilot/evaluation.py) | 划分数据集、生成盲审模板、计算评估指标、核验结果并比较两次运行。 |
| [common.py](../src/privacy_pilot/common.py) | 提供共用的标签定义、文件读写、哈希计算和异常类型。 |
| [\_\_init\_\_.py](../src/privacy_pilot/__init__.py) | 标识 Python 包并保存项目版本号。 |

### 准备输入

[source.py](../src/privacy_pilot/source.py) 负责下载校验和原始数据读取，支持 JSONL、gzip 和 ZIP 中的逐行记录。准备阶段清理无效记录、去重，并为保留的文本记录数据集版本、文件、ZIP 成员、行号和原始 ID 等来源信息。

准备结果包含正文文件 `input.jsonl`、来源清单 `source_manifest.json` 和排除记录 `excluded.jsonl`。原文和输入文件都保存哈希，便于后续检查内容是否变化。来源记录 ID 根据来源信息计算，模型不负责生成 ID 或原帖链接。项目使用公开离线语料，没有实时 Reddit 抓取器；自行收集的 JSONL 也可通过同一读取流程准备。

### 筛选候选并调用模型

[pipeline.py](../src/privacy_pilot/pipeline.py) 读取准备好的输入，逐条调用 [rules.py](../src/privacy_pilot/rules.py) 筛选候选。规则使用正则表达式和第一人称表达等线索；未命中的文本记为 `rule_negative`，命中的文本进入模型处理。较长的正文按带重叠的片段拆分，处理完后再合并回原记录。

[model.py](../src/privacy_pilot/model.py) 负责发送请求、处理有限重试、统计调用预算以及读写响应缓存。默认 DeepSeek 请求开启 JSON 输出并关闭思考模式；模块也支持兼容接口和 Ollama。HTTP 请求不跟随重定向，避免鉴权信息被转发到其他地址。

API Key 由 [credentials.py](../src/privacy_pilot/credentials.py) 在第一次实际请求时读取，顺序为环境变量、项目根目录 `.env`、终端隐藏输入。Key 只用于鉴权，不写入模型缓存或配置快照。

### 校验证据并复核

模型返回是否存在披露、是否需要进一步审阅，以及具体发现：

- `has_privacy`：是否包含隐私披露。
- `review_required`：是否仍有无法确认的歧义。
- `findings`：每项包含 `tag`、`evidence`、`subject`、`time`、`assertion` 和 `reason`。

[schema.py](../src/privacy_pilot/schema.py) 检查字段、标签和主体约束，并确认每段证据是当前文本片段中唯一出现的连续原文。证据位置 `start`、`end` 由程序计算，使用 Python Unicode 字符索引和左闭右开区间；长文本的片段位置会转换为全文位置。

校验失败时，`pipeline.py` 将错误反馈给模型，在配置允许的次数内要求修复。开启复核后，初次判断为正例或存在歧义的片段会再次交给模型检查。多个片段的发现按标签、位置、主体、时间和断言去重，每个最终标签都有证据，同一条原始文本只计一条结果。

### 导出与评估

通过自动检查的正例写入 `privacy_dataset.jsonl` 和 `privacy_dataset.csv`；所有输入的处理状态写入 `predictions.jsonl`，数量、类别分布和调用统计写入 `summary.json`。

[evaluation.py](../src/privacy_pilot/evaluation.py) 负责来源核验、人工审计和指标计算。最终正例的抽样审计用于检查有效率和标签精确率，独立测试集用于计算 Precision、Recall 和 F1。同一模型的复核可能重复原有错误，证据出现在原文中也不保证语义判断正确，因此质量评估仍需要人工标注。

[common.py](../src/privacy_pilot/common.py) 提供各模块共用的标签列表、JSON 读写、哈希计算和异常类型；`__init__.py` 保存项目版本号。

## prompts 如何约束模型

提示词单独保存在 [prompts/](../prompts/)，方便阅读和调整分类口径。

| 文件 | 用途 |
|---|---|
| [extract.txt](../prompts/extract.txt) | 定义 12 类标签、本人信息与他人信息的边界、原文证据要求和 JSON 输出格式，并提供构造示例 |
| [review.txt](../prompts/review.txt) | 要求模型对照原文检查已有抽取，删除错误发现、修正证据，并补充有明确依据的遗漏 |

复核时，程序将 `extract.txt` 与 `review.txt` 拼接，向模型同时提供原文和初次抽取结果，使两次判断使用相同的标签定义。提示词要求把 Reddit 正文当作待分析数据，不能执行正文中的指令，也不能根据姓名、职业等线索推断未明确披露的信息。

每次运行都会把实际使用的提示词复制到输出目录的 `prompts/` 中。复现时使用这份快照，可避免后续修改提示词影响旧实验。

## 结果、缓存与复现

README 中的 `artifacts/run-v1/` 保存一次运行的结果，`artifacts/cache-v1/` 保存可复用的模型请求和响应。配置快照、来源清单、提示词快照及 `cache_index.json` 则记录这次运行用了哪些输入和缓存。

缓存按接口协议、地址、处理阶段和完整请求内容匹配，请求中包含模型、提示词和推理参数。读取缓存时还会检查响应校验和。使用 `resume` 模式会复用已有响应，缺少时才请求模型；使用 `replay` 模式只读缓存，缺失即报错，不联网，也不读取 API Key。

运行指纹包含项目版本、实际配置、输入哈希、提示词和规则版本。使用同一版本的代码及对应输入、配置、提示词和完整缓存，可以重放处理流程，并通过 `compare` 检查最终 JSONL 是否逐字节一致。耗时和缓存命中次数等运行统计可以不同。

## 异常记录与测试

如果长文本的任一片段失败，程序不会导出该条文本的部分标签。仍有歧义的记录标为 `needs_review`，保留建议证据供审阅；超过分块上限的记录标为 `skipped_too_long`。这些状态在预测文件中单独记录。

普通记录处理失败后，流程继续处理下一条。鉴权失败或调用预算耗尽时，程序停止发起请求，并为余下记录写入未处理状态，便于之后使用缓存重跑。

[tests/](../tests/) 使用构造文本和模拟模型响应检查数据准备、证据校验、异常处理、人工评估与缓存复现；部分集成测试使用本地 HTTP 服务验证请求行为。测试不调用付费模型，具体记录见 [验证记录](validation.md)。标签定义和边界示例见 [标注规范](labeling_guidelines.md)。
