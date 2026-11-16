*AI3602 Pilot Project*

# Reddit 用户隐私信息挖掘

本项目从 [Webis-TLDR-17](https://zenodo.org/records/1043504) 的 Reddit 正文中识别 **12 类用户隐私信息**，采用**规则筛选与大模型多标签抽取**相结合的方法。项目要求详见 [先导作业说明](docs/pilot_project.md)。实现细节见 [架构说明](docs/architecture.md)，类别边界见 [标注规范](docs/labeling_guidelines.md)，工程测试见 [验证记录](docs/validation.md)。

***流程***

首先清洗、去重原始文本，保留正文和来源位置；再利用正则表达式和第一人称表达等线索筛选候选。随后调用 DeepSeek，根据上下文判断用户是否披露个人信息，为每条文本输出一个或多个类别标签，并摘录对应的原文作为证据。

***复核***

抽取结果经过程序校验和模型二次复核：程序检查标签是否合法、证据能否在原文中准确定位；模型复核信息是否属于作者本人及分类是否恰当。

***结果***

通过检查的记录导出为 JSONL 和 CSV，再通过人工抽样审计与独立测试集评估质量。

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

## 2. 填写 DeepSeek API Key

打开项目根目录的 `.env`，填写并保存：

```dotenv
DEEPSEEK_API_KEY=在这里填写你的DeepSeek_API_Key
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

默认模型和请求地址已写在 `config/default.toml` 中。

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

`--output artifacts/run-v1` 指定本次运行结果的保存目录；
`--cache artifacts/cache-v1` 指定模型请求和响应的缓存目录，首次运行时自动创建。缓存保存请求中的文本、提示词、模型参数和原始响应。缓存主要用于：

- **中断续跑**：`resume` 模式优先读取已有响应，只对未命中缓存的请求调用 DeepSeek，减少重复请求的时间和费用。
- **离线复现**：`replay` 模式读取缓存，重新执行解析、证据校验和结果导出，无需联网或 API Key。

程序筛选候选文本，调用 DeepSeek 抽取和复核，检查证据是否来自原文，再导出结果。终端持续显示处理进度，结束时输出汇总。**本步骤会产生 API 调用费用。**

主要结果保存在 `artifacts/run-v1/`：

| 文件 | 查看内容 |
|---|---|
| `privacy_dataset.jsonl` | 通过自动检查的正例，含完整正文、标签、证据和来源 |
| `privacy_dataset.csv` | 便于浏览的结果摘要 |
| `predictions.jsonl` | 每条输入的处理状态，包括负例和失败记录 |
| `summary.json` | 正例数、类别分布、调用统计及完成状态 |
| `config.snapshot.json`、`prompts/` | 本次配置与提示词快照 |
| `source_manifest.json`、`cache_index.json` | 来源清单和本次使用的缓存索引 |

检查 `summary.json` 中的 `counts.accepted` 和 `category_coverage`。`count_thresholds_met=true` 表示达到至少 50 条、至少 3 类且未标记为合成数据的数量条件；标签是否正确仍需第 8 步人工审计。

默认 `target_count=0`，处理全部输入；`max_calls=1500` 限制单次执行的请求尝试数，重试也计入。预算可能不足以处理全部样本，遇到 `unprocessed_budget` 时按文末方法继续。`complete_analysis=false` 表示还有失败、歧义、跳过或未处理记录，可在 `predictions.jsonl` 中定位。

> 如需先调试，可把 `--prepared` 改为 `data/prepared/splits-v1/dev`，同时换用开发运行目录。

## 7. 核验来源、证据和数量

Windows（PowerShell）：

```powershell
python run.py verify --run artifacts/run-v1 --raw-root data/raw --require-minimum --output artifacts/run-v1/verification.json
```

Linux（Bash）：

```bash
python3 run.py verify --run artifacts/run-v1 --raw-root data/raw --require-minimum --output artifacts/run-v1/verification.json
```

程序重新计算源文件哈希，核对原文位置、记录 ID、证据偏移和结果数量。通过时，输出中的 `structurally_valid`、`source_verification.ok` 和 `count_thresholds_met` 均为 `true`，`errors` 为空。

完整结果写入 `verification.json`。若失败，先按错误信息处理；省略 `--raw-root` 只检查结构，无法核验原始来源。

## 8. 人工审计最终数据

抽取最多 50 条正例，生成不展示模型标签的标注模板：

Windows（PowerShell）：

```powershell
python run.py audit --input artifacts/run-v1/privacy_dataset.jsonl --scope final --count 50 --seed 3602 --output artifacts/run-v1/evaluation/audit-final.jsonl
```

Linux（Bash）：

```bash
python3 run.py audit --input artifacts/run-v1/privacy_dataset.jsonl --scope final --count 50 --seed 3602 --output artifacts/run-v1/evaluation/audit-final.jsonl
```

输出标注文件和配套的 `.manifest.json`。按照 [标注规范](docs/labeling_guidelines.md) 逐行填写：

| 字段 | 填写方法 |
|---|---|
| `gold_labels` | 人工判断的类别，如 `["AGE", "OCC"]`；无披露填 `[]`，未审阅保留 `null` |
| `annotation_seconds` | 实际标注耗时，单位秒 |
| `source_verified` | 回查来源一致填 `true`，不符填 `false`，未核验保留 `null` |
| `notes` | 错误、边界情况或备注 |

保留原有文本、记录 ID 和来源字段。所有样本完成标注后执行：

Windows（PowerShell）：

```powershell
python run.py evaluate --predictions artifacts/run-v1/predictions.jsonl --gold artifacts/run-v1/evaluation/audit-final.jsonl --output artifacts/run-v1/evaluation/metrics-final.json
```

Linux（Bash）：

```bash
python3 run.py evaluate --predictions artifacts/run-v1/predictions.jsonl --gold artifacts/run-v1/evaluation/audit-final.jsonl --output artifacts/run-v1/evaluation/metrics-final.json
```

输出文本有效率、标签精确率、标签集合完全正确率及来源核验情况。这些指标只针对最终正例抽样，总体召回率和 F1 使用下一步的独立测试集计算。人工审计不会自动修改已导出的数据。

## 9. 评估独立测试集

先导出测试集的盲审模板，按第 8 步填写完毕后再查看模型预测：

Windows（PowerShell）：

```powershell
python run.py audit --input data/prepared/splits-v1/test/input.jsonl --scope holdout --count 100 --seed 3602 --output artifacts/run-v1/evaluation/gold-test.jsonl
```

Linux（Bash）：

```bash
python3 run.py audit --input data/prepared/splits-v1/test/input.jsonl --scope holdout --count 100 --seed 3602 --output artifacts/run-v1/evaluation/gold-test.jsonl
```

用正式运行时的配置和提示词处理测试集，再计算指标：

Windows（PowerShell）：

```powershell
python run.py run --prepared data/prepared/splits-v1/test --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/test-v1 --cache artifacts/cache-v1 --mode resume
python run.py evaluate --predictions artifacts/test-v1/predictions.jsonl --gold artifacts/run-v1/evaluation/gold-test.jsonl --output artifacts/run-v1/evaluation/metrics-test.json
```

Linux（Bash）：

```bash
python3 run.py run --prepared data/prepared/splits-v1/test --config artifacts/run-v1/config.snapshot.json --prompts artifacts/run-v1/prompts --output artifacts/test-v1 --cache artifacts/cache-v1 --mode resume
python3 run.py evaluate --predictions artifacts/test-v1/predictions.jsonl --gold artifacts/run-v1/evaluation/gold-test.jsonl --output artifacts/run-v1/evaluation/metrics-test.json
```

`metrics-test.json` 包含 Micro Precision、Recall、F1 和各类别指标；分母为零时对应值为 `null`。测试配置须保持 `target_count=0`。未完成的人工标签、缺失预测或调用失败会导致评估报错，需补齐后重算。`needs_review` 按弃权处理，金标中的相应类别计为漏检。

测试集不用于调参。

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

## 中断恢复与常见问题

| 情况 | 处理方法 |
|---|---|
| 下载中断，有 `.part` 文件 | 重复执行原 `download` 命令，尝试续传 |
| 需要从头下载 | 在 `download` 命令后加 `--restart`，程序先备份旧片段 |
| 完整 ZIP 校验失败 | 保留文件，换输出路径重新下载或传输 |
| `prepare`、`split` 或 `run` 提示输出目录已存在 | 换用新目录，例如将 `v1` 改成 `v2` |
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
