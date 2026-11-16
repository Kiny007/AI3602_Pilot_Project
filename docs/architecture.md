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
│   ├── judge.toml           # GPT API
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

`config/default.toml` 保存默认设置，包括输入范围、DeepSeek 模型、调用预算、长文本分块大小和是否复核。`config/ollama.toml` 提供本地模型的配置示例。`config/judge.toml` 单独配置 GPT 判分与复核模型。

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

模型返回：

- `has_privacy`：是否包含隐私披露。
- `review_required`：是否仍有无法确认的歧义。
- `findings`：每项包含 `tag`、`evidence`、`subject`、`time`、`assertion` 和 `reason`。

[schema.py](../src/privacy_pilot/schema.py) 检查字段、标签和主体约束，并确认每段证据是当前文本片段中唯一出现的连续原文。校验失败时，`pipeline.py` 将错误反馈给模型，在配置允许的次数内要求修复。

开启复核后，初次判断为正例或存在歧义的片段会再次交给模型检查。多个片段的发现按标签、位置、主体、时间和断言去重，每个最终标签都有证据，同一条原始文本只计一条结果。

### 导出与评估

通过自动检查的正例写入 `privacy_dataset.jsonl` 和 `privacy_dataset.csv`；所有输入的处理状态写入 `predictions.jsonl`，数量、类别分布和调用统计写入 `summary.json`。

[judge.py](../src/privacy_pilot/judge.py) 对固定样本执行 LLM-as-a-Judge 判分。它只把原文和标签规范交给 GPT，不提供 DeepSeek 的预测标签；随后用配置的复核模型检查 GPT 自己的结果。

[evaluation.py](../src/privacy_pilot/evaluation.py) 将 DeepSeek 预测与 GPT 参考标签比较。最终正例抽样只计算文本有效率、标签精确率和集合一致率；独立测试集额外计算 Precision、Recall 和 F1。

## prompts 约束模型

提示词单独保存在 `prompts` 文件夹。

| 文件 | 用途 |
|---|---|
| [extract.txt](../prompts/extract.txt) | 定义 12 类标签、本人信息与他人信息的边界、原文证据要求和 JSON 输出格式，并提供构造示例 |
| [review.txt](../prompts/review.txt) | 要求模型对照原文检查已有抽取，删除错误发现、修正证据，并补充有明确依据的遗漏 |
| [judge.txt](../prompts/judge.txt) | 要求 GPT 在看不到 DeepSeek 标签的情况下，独立给出参考标签及原文证据 |
| [judge_review.txt](../prompts/judge_review.txt) | 对 GPT 参考标注再做检查，包括负例中的遗漏与无法消除的歧义 |

复核时，程序将 `extract.txt` 与 `review.txt` 拼接，向模型同时提供原文和初次抽取结果，使两次判断使用相同的标签定义。

GPT 判分使用 `extract.txt` 中相同的标签定义，叠加 `judge.txt`；复核再叠加 `judge_review.txt`。判分不调用搜索工具，也不根据外部信息补全个人事实。


## 异常记录与测试

- 如果长文本的任一片段失败，程序不会导出该条文本的部分标签。
- 仍有歧义的记录标为 `needs_review`，保留建议证据供审阅；超过分块上限的记录标为 `skipped_too_long`。
- 普通记录处理失败后，流程继续处理下一条。
- 鉴权失败或调用预算耗尽时，程序停止发起请求，并为余下记录写入未处理状态，便于之后使用缓存重跑。

项目测试详见 [验证记录](/docs/validation.md)。
