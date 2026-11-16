*AI3602 Pilot Project*

# Reddit 用户隐私信息挖掘

本项目从 [Webis-TLDR-17](https://zenodo.org/records/1043504) 的 Reddit 正文中识别 **12 类用户隐私信息**，采用**规则筛选与大模型多标签抽取**相结合的方法。项目要求详见 [先导作业说明](docs/pilot_project.md)。实现细节见 [架构说明](docs/architecture.md)，类别边界见 [标注规范](docs/labeling_guidelines.md)，工程测试见 [验证记录](docs/validation.md)。

***流程***

首先清洗、去重原始文本，保留正文和来源位置；再利用正则表达式和第一人称表达等线索筛选候选。随后调用 DeepSeek，根据上下文判断用户是否披露个人信息，为每条文本输出一个或多个类别标签，并摘录对应的原文作为证据。

***复核***

抽取结果经过程序校验和模型二次复核：程序检查标签是否合法、证据能否在原文中准确定位；模型复核信息是否属于作者本人及分类是否恰当。

***结果***

通过检查的记录导出为 JSONL 和 CSV，再由 GPT 以 LLM-as-a-Judge 方式评估抽样结果和独立测试集。

***复现***

首次运行按第 1 至 10 步操作；已有运行结果和缓存时，可直接使用第 10 步的离线复现命令。

## 1. 检查运行环境

需要 Python 3.11 或以上，所有命令均在项目根目录执行。

> Windows & Linux

```powershell
python --version
python run.py --help
python -m unittest discover -s tests -v
```

依次检查 **Python 版本、查看可用子命令、运行测试**。测试只检查程序逻辑，成功时末尾显示 `OK`；

使用 uv 时，可将 `python run.py` 换成 `uv run run.py`。

## 2. 配置抽取与判分 API Key

打开项目根目录的 `.env`，填写并保存：

```dotenv
DEEPSEEK_API_KEY=在这里填写你的DeepSeek_API_Key
JUDGE_API_KEY=在这里填写你的GPT_API_Key
```

如果还没有 `.env`，先执行：

> Windows

```powershell
Copy-Item .env.example .env
```

> Linux
> 
```bash
cp -i .env.example .env
```

DeepSeek 抽取配置在 `config/default.toml`；GPT 判分配置在 `config/judge.toml`，判分只依据原文，不启用搜索工具。

## 3. 下载数据

数据使用 [Webis-TLDR-17 的 Zenodo 发布包](https://zenodo.org/records/1043504)。在本地项目根目录执行以下命令，程序会自动创建目录并下载到 `data/raw/corpus-webis-tldr-17.zip`：

> Windows & Linux

```powershell
python run.py download --output data/raw/corpus-webis-tldr-17.zip
```

下载过程中显示进度，完成后校验发布方提供的 MD5，并生成 `corpus-webis-tldr-17.zip.source.json`，记录来源和文件哈希。

根据本地文件状态，成功时会返回以下状态之一：

| 状态 | 含义 |
|---|---|
| `downloaded` | 本次下载完成并通过校验 |
| `already_complete` | 已有完整 ZIP，通过校验后直接复用|
| `recovered_complete` | 已有完整的 `.part` 文件，通过校验后恢复为 ZIP |

*中断后重新执行同一条命令即可尝试续传。程序直接读取 ZIP 中的 `body` 正文，无需解压。*

## 4. 清洗数据

> Windows & Linux

```bash
python run.py prepare --input data/raw/corpus-webis-tldr-17.zip --output data/prepared/webis-v1
```

默认最多扫描 5,000 条非空输入行，同时清理无效记录并去重。终端输出来源清单及计数，目录内生成：

| 文件 | 内容 |
|---|---|
| `input.jsonl` | 清洗后的正文与来源定位 |
| `source_manifest.json` | 数据版本、扫描范围、文件哈希与保留数量 |
| `excluded.jsonl` | 被排除的记录及原因 |

## 5. 划分样本 

在规则筛选和模型调用前划分数据：

> Windows & Linux

```bash
python run.py split --prepared data/prepared/webis-v1 --output data/prepared/splits-v1 --dev 40 --test 100 --seed 3602
```

输出 `dev`、`test`、`collection` 三个目录及各自条数。开发集 40 条用于调试，测试集 100 条留作独立评估，其余用于收集最终数据。

## 6. 运行隐私挖掘

> Windows & Linux

```bash
python run.py run --prepared data/prepared/splits-v1/collection --output artifacts/run-v1 --cache artifacts/cache-v1 --mode resume
```

>默认 `target_count=0`，处理全部输入；`max_calls=1500` 限制单次执行的 api 请求尝试数，重试也计入。

> 如需先调试，可把 `--prepared` 改为 `data/prepared/splits-v1/dev`，同时换用开发运行目录。

`--output artifacts/run-v1` 指定本次运行结果的保存目录；
`--cache artifacts/cache-v1` 指定模型请求和响应的缓存目录，首次运行时自动创建。缓存保存请求中的文本、提示词、模型参数和原始响应。缓存主要用于：

- **中断续跑**：`resume` 模式优先读取已有响应，只对未命中缓存的请求调用 DeepSeek，减少重复请求的时间和费用。
- **离线复现**：`replay` 模式读取缓存，重新执行解析、证据校验和结果导出，无需联网或 API Key。

程序筛选候选文本，调用 DeepSeek 抽取和复核，检查证据是否来自原文，再导出结果。终端持续显示处理进度，结束时输出汇总。**本步骤会产生 API 调用费用。**

主要结果保存在 `artifacts/run-v1/`：

| 文件 | 查看内容 |
|---|---|
| `privacy_dataset.jsonl` | 通过自动检查的正例，含完整正文、标签、证据和来源 |
| `privacy_dataset.csv` | 便于浏览的结果 |
| `predictions.jsonl` | 每条输入的处理状态，包括负例和失败记录 |
| `summary.json` | 正例数、类别分布、调用统计及完成状态 |
| `config.snapshot.json`、`prompts/` | 本次配置与提示词快照 |
| `source_manifest.json`、`cache_index.json` | 来源清单和本次使用的缓存索引 |

检查 `summary.json` 中的 `counts.accepted` 和 `category_coverage`。`count_thresholds_met=true` 表示达到 *至少 50 条、至少 3 类且未标记为合成数据* 的数量条件。


## 7. 核验来源、证据和数量

> Windows & Linux

```bash
python run.py verify --run artifacts/run-v1 --raw-root data/raw --require-minimum --output artifacts/run-v1/verification.json
```

程序重新计算源文件哈希，核对原文位置、记录 ID、证据偏移和结果数量。

通过时，输出中的 `structurally_valid`、`source_verification.ok` 和 `count_thresholds_met` 均为 `true`，`errors` 为空。

## 8. LLM-as-a-Judge 评估最终数据

使用 GPT 独立判断抽样文本，再与 DeepSeek 的标签比较。默认从最终正例中随机抽取最多 50 条，固定种子为 3602。GPT 只收到原文和标签规范，不会看到 DeepSeek 的标签、理由或预测状态；第二次复核只检查 GPT 自己的判断。

> Windows & Linux
```bash
python run.py judge --input artifacts/run-v1/privacy_dataset.jsonl --scope final --count 50 --seed 3602 --output artifacts/judge-final-v1 --cache artifacts/judge-cache-v1 --mode resume
```

判分使用 `config/judge.toml`，初次判断和复核模型均为 `gpt-6.1-sol`，推理强度单独设置为 `reasoning_effort=high`。结果保存在 `artifacts/judge-final-v1/`：

| 文件 | 内容 |
|---|---|
| `sample.jsonl`、`sample.manifest.json` | 固定抽样的原文、来源、种子和输入哈希 |
| `judgments.jsonl` | GPT 参考标签、原文证据、判断状态和模型信息 |
| `summary.json` | 完成数量、失败或歧义数量、调用统计 |
| `config.snapshot.json`、`prompts/`、`cache_index.json` | 本次判分配置、提示词快照和缓存索引 |

`summary.json` 中 `complete=true` 表示全部抽样文本都有明确的参考标签。如果只有 `ok` 和 `needs_review` 两种状态，也可以继续评估：只统计判分明确的记录，将仍有歧义的记录计为 GPT 弃权，保留原始抽样文件。此时 `complete` 仍为 `false`，`judge` 命令会返回退出码 2，但可以单独运行下面的评分命令，无需再次请求 GPT：

```bash
python run.py evaluate --predictions artifacts/run-v1/predictions.jsonl --gold artifacts/judge-final-v1/judgments.jsonl --output artifacts/run-v1/evaluation/metrics-final-gpt.json
```

输出相对于 GPT 参考标签的文本有效率、标签精确率和标签集合完全一致率。指标带有 `reference_kind=llm_judge`、`reference_is_human=false`，不作为人工标注准确率；仅抽取最终正例也无法计算总体召回率或 F1。沿用的字段名 `gold_labels` 和参数名 `--gold` 在这里表示模型参考标签。

评估文件同时记录原始抽样数、实际评分数和弃权情况。例如，50 条样本中有 47 条判分明确、3 条仍有歧义时：

| 字段 | 含义 | 示例值 |
|---|---|---:|
| `sample_size` | 原始抽样文本数 | 50 |
| `evaluated_count` | 实际参与指标计算的文本数 | 47 |
| `judge_coverage` | 明确判分数 / 原始抽样数 | 0.94（94%） |
| `judge_abstained_count` | GPT 因歧义弃权的文本数 | 3 |
| `judge_abstention_rate` | GPT 弃权比例 | 0.06（6%） |

此时文本有效率和集合一致率的分母为 47，标签精确率也只汇总这 47 条中的标签。**报告指标时须同时写明 94% 的判分覆盖率和 3 条弃权，不能将指标当作全部 50 条的结果。** 弃权文本可能更难判断，因此也不能据此假定它们与已评分文本表现相同。`judge_abstentions` 保存弃权记录的 ID 和原因，`metrics_scope=resolved_judgments_only` 标明指标范围；若全部弃权，各项质量比率为 `null`。

GPT 仍可能误判，来源真实性由第 7 步核验。判分不改写原始抽取结果，也不填写人工标注耗时或来源核验结论。请求固定 `store=false`，本地仍保留响应缓存用于续跑和复现；这不代表对第三方网关日志策略的保证。判分不使用网页搜索，避免引入原文之外的信息。

## 9. LLM-as-a-Judge 评估独立测试集

使用第 5 步保留的 100 条测试文本，包含正例与负例。GPT 判分会处理全部抽样文本，不使用 DeepSeek 的规则过滤：

```bash
python run.py judge --input data/prepared/splits-v1/test/input.jsonl --scope holdout --count 100 --seed 3602 --output artifacts/judge-test-v1 --cache artifacts/judge-cache-v1 --mode resume
```

随后使用正式抽取时的 DeepSeek 配置和提示词处理同一测试集，并与 GPT 参考标签比较：

```bash
python run.py run --prepared data/prepared/splits-v1/test --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/test-v1 --cache artifacts/cache-v1 --mode resume
python run.py evaluate --predictions artifacts/test-v1/predictions.jsonl --gold artifacts/judge-test-v1/judgments.jsonl --output artifacts/run-v1/evaluation/metrics-test-gpt.json
```

`metrics-test-gpt.json` 包含相对于 GPT 参考标签的 Micro Precision、Recall、F1、各类别指标和标签集合完全一致率。分母为零时返回 `null`。这些结果衡量两个模型在固定标签规范下的差异，不等于人工验证后的真实准确率。

测试配置须保持 `target_count=0`，测试集不用于调参。对 GPT 已明确判分的文本，抽取端的 `needs_review` 作为无预测标签处理，参考标签中的对应类别计为漏检；`abstained_count` 记录这些抽取端弃权。GPT 判分端的 `needs_review` 则由 `judge_abstained_count` 单独统计，不参与质量指标计算，也不当作负例。测试集同样须同时报告判分覆盖率、弃权数和实际评分数，指标仅适用于明确判分的子集。

拒答、无效证据、预算耗尽或其他失败不属于上述歧义弃权，仍会阻止评分。评分也会校验弃权记录的文本、来源、证据和预测是否完整，不通过删除记录来绕过检查。

### GPT 判分续跑与离线复现

判分输出目录必须是新目录。中断或预算耗尽后，保留 `judge-cache-v1`，以原配置、提示词和抽样参数重跑：

```bash
python run.py judge --input artifacts/run-v1/privacy_dataset.jsonl --scope final --count 50 --seed 3602 --config artifacts/judge-final-v1/config.snapshot.json --prompts artifacts/judge-final-v1/prompts --output artifacts/judge-final-v2 --cache artifacts/judge-cache-v1 --mode resume
```

每次执行重新计算请求次数额度，默认上限 1,500，初次判断、复核、修复和 HTTP 重试共用这份额度。续跑可能继续产生费用；已缓存的错误响应不会因重跑自动变正确，持续歧义或校验失败需要检查提示词、输出长度等设置后，使用新目录记录修订实验。

缓存完整时，将 `--mode resume` 改成 `--mode replay` 并使用新输出目录，即可离线重放判分。参考标签、证据和请求轨迹应一致，`judge_seconds` 等耗时可以不同；`compare` 命令只用于第 10 步的抽取结果，不用于判分目录。测试集判分续跑时对应替换输入路径、`scope=holdout`、`count=100` 和快照目录。使用新判分结果后，`evaluate --gold` 也要指向新的 `judgments.jsonl`。

## 10. 离线重放并比较结果

Windows（PowerShell）：

```powershell
python run.py run --prepared data/prepared/splits-v1/collection --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/replay-v1 --cache artifacts/cache-v1 --mode replay
python run.py compare artifacts/run-v1 artifacts/replay-v1
```

Linux（Bash）：

```bash
python3 run.py run --prepared data/prepared/splits-v1/collection --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/replay-v1 --cache artifacts/cache-v1 --mode replay
python3 run.py compare artifacts/run-v1 artifacts/replay-v1
```

`replay` 只读取缓存，不需要 API Key，也不联网。它重新执行解析、校验和导出；缺少缓存时会报错。

使用相同代码、输入、配置及提示词时，比较结果应为 `"identical_bytes": true`，表示最终 `privacy_dataset.jsonl` 逐字节一致。耗时、缓存命中等运行统计可以不同。若正式运行尚有未处理记录，应先完成运行再重放。

---

## 中断恢复与常见问题

| 情况 | 处理方法 |
|---|---|
| 下载中断，有 `.part` 文件 | 重复执行原 `download` 命令，尝试续传 |
| 需要从头下载 | 在 `download` 命令后加 `--restart`，程序先备份旧片段 |
| 完整 ZIP 校验失败 | 保留文件，换输出路径重新下载或传输 |
| `prepare`、`split`、`run` 或 `judge` 提示输出目录已存在 | 换用新目录，例如将 `v1` 改成 `v2` |
| API 鉴权失败 | 检查 `.env` 和同名环境变量；环境变量优先 |
| 运行中断或出现 `unprocessed_budget` | 使用原缓存、新输出目录，以 `resume` 模式重跑 |
| `replay` 缺少缓存 | 找回该次运行的完整缓存，或用 `resume` 补充模型调用后重新核验 |
| 数量或类别不足 | 扩大原始输入范围，重新准备和运行，再核验结果 |

例如，继续使用 `run-v1` 的配置、提示词和已完成缓存：

Windows（PowerShell）：

```powershell
python run.py run --prepared data/prepared/splits-v1/collection --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/run-v2 --cache artifacts/cache-v1 --mode resume
```

Linux（Bash）：

```bash
python3 run.py run --prepared data/prepared/splits-v1/collection --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/run-v2 --cache artifacts/cache-v1 --mode resume
```

命中缓存的请求直接复用，其余继续调用 API。请求预算按每次执行重新计数，可能继续产生费用。后续核验、评估和比较命令中的正式运行目录也要改为 `artifacts/run-v2`。

如果需要重新请求模型做对照实验，使用新输出目录、新缓存目录和 `--mode fresh`；重新调用模型可能得到不同结果。
