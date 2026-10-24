# 专题：把 Jev 接入 teach_agent 的教材小节映射

> 实验报告 + 可复现 demo。代码基于原项目 `teach_agent` 的**副本**
> `teach_agent_jev` 实现，原项目未改动。

## 0. 摘要

在「课堂主题 → 教材小节」映射链路中，原有排序只依赖向量相似度，容易出现
「距离最近 = 最相关」的误判。本实验在不改变召回范围、不新增数据库列的前提下，
把 Jev（typesafe.ai System One）作为**重排判定器**接入，对前 6 个候选小节做
3 档 Score 判定后重新排序。

实验结果：

- 真实 Jev 能把纯向量误判的干扰小节（fit=0）排除，改选真正讲授该主题的小节；
- 教材确实没有的主题，全候选 fit=0，映射落到 `needs_review`，不硬凑；
- 人工已审核（approved/rejected）的映射完全不被重排覆盖；
- Jev 超时/报错/schema 不符时自动回落向量排序，不影响主流程；
- 默认 `vector` 行为零变化，44 个单元测试全通过。

## 1. 实验目标

| 目标 | 验证方式 |
| --- | --- |
| Jev 能纠正向量误判 | 构造「向量最像但内容无关」的干扰小节 |
| 不扩大候选范围 | 只对向量召回的前 K=6 个候选判定 |
| 不破坏人工审核 | 先 approve 一条，再开启 Jev 重排 |
| 失败可降级 | 注入异常判定器，观察是否回落 |
| 默认行为不变 | `judge=None` 时与 v1 排序结果一致 |

## 2. 实验环境

| 项目 | 值 |
| --- | --- |
| 端点 | `https://api.typesafe.ai/v1/systemone` |
| 模型 | `jev-latest`（服务端自报 `jev-1.14.0`） |
| 认证 | `TEACH_JEV_API_KEY`，未设置时回退 `TYPESAFE_API_KEY` |
| 单问延迟 | 91–1115 ms（正常链路） |
| 5 候选载荷 | 约 4.1 KB |
| 业务库 | SQLite 内存库（demo），生产为 MySQL |
| 依赖 | 仅标准库 `urllib`，未新增第三方包 |

## 3. 方案设计

### 3.1 链路

```text
课堂主题 + aliases + lecture.summary
        │
        ▼
向量检索（原有逻辑，范围 = 课程绑定的教材）
        │  候选 chunk → 所属 BookSection
        ▼
按小节聚合，取最高向量分 + 多段命中奖励
        │
        ▼
取前 K=6 个小节（不扩召回）
        │
        ▼
Jev Score × 3 档（每候选一题）
        │
        ├── 失败 / schema 不符 → 回落向量排序，记 jev_fallback
        ▼
按 (fit, 向量分) 重排
        │
        ├── fit = 0        → section_id = None，needs_review
        ├── approved/rejected → 跳过重排，原样返回
        ▼
写入 TopicMapping（mapper_version = section-map-v2）
```

### 3.2 判定问题

三类问题类型中选用 `score`（序数量表）：

```python
JUDGE_CRITERIA = ("没有讲该主题", "相关但不是同一主题", "直接讲该主题")
```

返回的 `score` 是 0–2 的浮点数（如 1.72），四舍五入为整数 fit。

### 3.3 排序与审核保护

- 排序键：`(fit, 向量分 + 标题加成)`；同分时记 `tie_break=vector`。
- `confidence` 仍等于向量相似度，语义不变，避免把 Jev 分数误当概率。
- `approved` / `rejected` 的行直接复用，**不重新检索、不重新判定**。
- `pending` / `needs_review` 可重排，但结果仍写 `pending`，绝不自动通过。
- 不新增数据库列：Jev 结果只进 `input_fingerprint` payload 与 `reasons`。

### 3.4 注入方式

沿用现有 `retrieve` 的依赖注入风格：

```python
Judge = Callable[[dict, dict], dict | None]

map_topics(engine, lecture_id, topics, retrieve=..., judge=None, judge_top_k=6)
```

CLI：`map --judge vector|jev`，缺省读取 `TEACH_MAPPING_JUDGE`。

## 4. 实验数据与设计

假想课程《模型评估》，教材第 2 章共 5 个小节。检索候选固定，便于复现：

| 小节 | 正文摘要 | distance | 向量分 |
| --- | --- | --- | --- |
| 2.3 习题课安排 | 习题课时间、答疑地点、作业提交要求 | 0.05 | **0.95** |
| 2.2.1 泛化误差 | 泛化误差分解为偏差、方差与噪声 | 0.18 | 0.82 |
| 2.2 泛化能力 | 模型对未知样本的预测能力 | 0.22 | 0.78 |
| 2.1 基本概念 | 训练误差、测试误差的定义 | 0.30 | 0.70 |
| 第2章 模型评估 | 评估目标、常用指标与流程 | 0.40 | 0.60 |

三个主题覆盖三种情形：

1. **需要纠正**：`模型对未见样本的预测表现` —— 向量最像的是干扰小节；
2. **审核保护**：`课程通知对应的背景知识` —— 先人工 approve；
3. **应当拒答**：`校园网账号如何重置` —— 教材确实没有该主题。

## 5. Demo（必须包含）

### 5.1 运行方式

```bash
cd /home/xiaoberber/agent_project/teach_agent_jev
cp .env.example .env          # 仅本地保存密钥，.env 不入库
.venv/bin/python scripts/demo_jev_mapping.py
```

演示使用内存 SQLite、固定章节树与固定检索候选，**只有判定环节走真实 Jev**，
因此业务逻辑完全可复现。输出中 `来源` 字段标明判定出处：

- `live`：本次真实调用；
- `recorded`：链路失败后回放 `scripts/jev_demo_recorded.json` 中一次真实响应。

产品客户端本身不重试、不读回放；重试与回放只存在于 demo 外壳。

### 5.2 真实运行输出

```text
== 向量基线（judge=vector） ==
主题                 小节                 置信度      状态
模型对未见样本的预测表现       2.3 习题课安排          0.95     pending
课程通知对应的背景知识        2.3 习题课安排          0.95     pending
校园网账号如何重置          2.3 习题课安排          0.95     pending

人工审核：课程通知对应的背景知识 → 2.3 习题课安排（approved）

== 真实 Jev 重排（judge=jev） ==
主题                 小节                 置信度      状态
模型对未见样本的预测表现       2.2.1 泛化误差         0.82     pending
课程通知对应的背景知识        2.3 习题课安排          0.95     approved
校园网账号如何重置          None               None     needs_review

== Jev 判定明细（调用 2 次）==

主题：模型对未见样本的预测表现  时延：847 ms  尝试：1  来源：live  状态：ok
  2.3 习题课安排   path=第2章 模型评估 > 2.3 习题课安排                  fit=0
  2.2.1 泛化误差   path=第2章 模型评估 > 2.2 泛化能力 > 2.2.1 泛化误差      fit=2
  2.2 泛化能力     path=第2章 模型评估 > 2.2 泛化能力                    fit=2
  2.1 基本概念     path=第2章 模型评估 > 2.1 基本概念                     fit=1
  第2章 模型评估   path=第2章 模型评估                                 fit=1

主题：校园网账号如何重置  时延：507 ms  尝试：1  来源：live  状态：ok
  2.3 习题课安排   path=第2章 模型评估 > 2.3 习题课安排                  fit=0
  2.2.1 泛化误差   path=第2章 模型评估 > 2.2 泛化能力 > 2.2.1 泛化误差      fit=0
  2.2 泛化能力     path=第2章 模型评估 > 2.2 泛化能力                    fit=0
  2.1 基本概念     path=第2章 模型评估 > 2.1 基本概念                     fit=0
  第2章 模型评估   path=第2章 模型评估                                 fit=0

== 审核保护 ==
课程通知对应的背景知识：section=2.3 习题课安排 status=approved id_unchanged=True

== 映射指纹（前 16 位；变化即触发下游计划 revision）==
模型对未见样本的预测表现  6549ae22e7e9c039 -> dcfd62f865488d1b (changed)
课程通知对应的背景知识   88d56180fdfcedb8 -> 88d56180fdfcedb8 (unchanged)
校园网账号如何重置      d0ece74f7e985b1f -> 6ef7b92ed259bfdf (changed)

mapper_version=['section-map-v2']
```

### 5.3 输出逐步解读

**① 向量基线**：三个主题全部落到 `2.3 习题课安排`（0.95）。原因只是它
`distance=0.05` 最近，与主题内容无关——纯向量排序的典型失败模式。

**② 人工审核**：把「课程通知对应的背景知识 → 2.3 习题课安排」置为 `approved`，
模拟老师已经确认过的映射。

**③ 重排结果**：

| 主题 | 变化 | 含义 |
| --- | --- | --- |
| 模型对未见样本的预测表现 | 2.3 习题课安排 → **2.2.1 泛化误差** | 干扰项被排除，纠正到真正讲授该主题的小节 |
| 课程通知对应的背景知识 | 保持 2.3 习题课安排，`approved` | 已审核映射未被覆盖 |
| 校园网账号如何重置 | `None` + `needs_review` | 教材确实没有该主题，拒绝硬凑 |

**④ 判定明细**：第一次调用 847 ms，干扰小节 fit=0；`2.2.1` 与 `2.2` 同为 fit=2，
按向量分（0.82 > 0.78）选中更具体的 `2.2.1`，并记录 `tie_break=vector`。
第二次调用 507 ms，五个候选全部 fit=0，因此拒答。

**⑤ 审核保护**：`approved` 行的映射 ID 与内容完全不变（`id_unchanged=True`），
本次重排甚至没有重新检索它。

**⑥ 指纹与版本**：被重排的主题指纹变化，会触发下游 `study_planner` 的新 revision；
已审核映射指纹不变；`mapper_version = section-map-v2`。

### 5.4 判定请求的核心代码

```python
questions = {
    f"candidate_{index}": {
        "type": "score",
        "instructions": {
            "question": (
                "这个教材小节实际讲授的内容与课堂主题的匹配程度如何？"
                "只有正文主体就在讲该主题时才判为“直接讲”；仅仅同属一章、"
                "或只在标题里出现相似词，都不算。"
            ),
            "topic": topic,
            "section_title": candidate["section_title"],
            "section_path": candidate.get("section_path"),
            "excerpt": candidate.get("excerpt"),
        },
        "criteria": list(JUDGE_CRITERIA),
    }
    for index, candidate in enumerate(candidates)
}
```

注意：候选内容必须写进**每一题自己的 instructions**，不能只放在 `state` 里
（原因见第 7 节）。

### 5.5 demo 外壳的两项保护

```python
# 重试 + 录制回放，仅用于 demo；产品客户端不重试
while tries < self.attempts and answers is None:
    tries += 1
    answers = self.inner(state, questions)

if answers is None:
    answers = self.recorded.get(state["topic"])
    source = "recorded" if answers is not None else "failed"
```

## 6. 实验结果

### 6.1 判定分数（真实响应）

主题「模型对未见样本的预测表现」某次调用的原始评分：

| 小节 | score | probabilities（0/1/2） | fit |
| --- | --- | --- | --- |
| 2.3 习题课安排 | 0.01 | 0.96 / 0.04 / 0.00 | 0 |
| 2.2.1 泛化误差 | 1.80 | 0.03 / 0.14 / 0.83 | 2 |
| 2.2 泛化能力 | 1.99 | 0.00 / 0.01 / 0.99 | 2 |
| 2.1 基本概念 | 0.90 | 0.14 / 0.82 / 0.04 | 1 |
| 第2章 模型评估 | 0.86 | 0.17 / 0.79 / 0.04 | 1 |

主题「校园网账号如何重置」全部候选 `score ≤ 0.01`，fit 均为 0。

### 6.2 指标

| 指标 | 向量基线 | Jev 重排 |
| --- | --- | --- |
| 干扰小节被选中 | 是（3/3 主题） | 否（0/2 判定的主题） |
| 教材外主题 | 错误绑定 | 正确拒答（needs_review） |
| 已审核映射被改动 | — | 0 |
| 额外延迟 | 0 | 约 0.5–1.1 s/主题 |
| 失败影响 | — | 自动回落，无异常抛出 |

## 7. 关键发现

1. **Jev 的问题 ID 不参与推理。** 最初把候选摘要放在 `state`，每题只发相同指令，
   结果 5 个候选全部被判 fit=2，干扰小节也没被排除。改为每题 `instructions`
   内嵌 `topic / section_title / section_path / excerpt` 后，判定立即出现区分度。
   **结论：判定条件必须自包含在问题内，不能依赖 `state` 的隐式引用。**
2. **fit=2 不代表唯一。** `2.2` 与 `2.2.1` 同为 fit=2，靠向量分兜底选更具体的子小节，
   并记录 `tie_break=vector`，保证结果可解释。
3. **拒答可用。** 教材确实没有的主题得到全 0，映射落到 `needs_review`，
   不会产生错误绑定。
4. **链路不稳定。** 本机代理偶发 `Connection reset by peer`（与密钥有效性无关），
   实测单次成功率约 50%。产品侧保持“失败即回落”；demo 重试两次后回放录制响应。
5. **指纹语义正确。** 重排会改变未审核映射的指纹，从而触发下游计划 revision；
   已审核映射指纹不变，避免无意义的重算。

## 8. 测试与静态检查

```text
pytest tests --ignore=tests/integration        → 44 passed
pytest tests/test_jev.py tests/test_curriculum.py → 12 passed
ruff check（curriculum / config / tests / scripts） → All checks passed
```

新增用例：

- 重排生效且指纹变化；
- 全 0 拒答 → `needs_review`；
- schema 不符 → 回落向量；
- `approved` 映射不重新检索、不重排；
- 平局按向量分兜底；
- Jev 客户端请求契约与异常回落（stub，不联网）。

## 9. 局限与后续

- 实验只有 3 个主题、5 个候选，属于机制验证，不代表整体准确率；
  后续需用真实课程导出候选做批量回放（见 `compare_mapping_judges.py`）。
- 未评测 Jev 判定的稳定性（同一输入重复调用的方差）。
- 开启 Jev 会使未审核映射指纹变化，首次上线建议在副本库上试跑。
- 密钥只应保存在本地 `.env`；仓库中不含任何密钥。

## 10. 文件清单

```text
code/
├── src/teach_agent/
│   ├── config.py                       # 新增 Jev 配置项
│   └── curriculum/
│       ├── jev.py                      # Jev 最小客户端 + 判定器工厂
│       ├── mapping.py                  # 重排、审核保护、降级
│       └── __main__.py                 # map --judge vector|jev
├── tests/
│   ├── test_jev.py                     # 客户端契约与异常
│   └── test_curriculum.py              # 重排/拒答/回落/审核保护/平局
└── scripts/
    ├── demo_jev_mapping.py             # 本文第 5 节的可运行 demo
    ├── jev_demo_recorded.json          # 一次真实响应，供链路失败时回放
    └── compare_mapping_judges.py       # 三方离线对比回放
```

这些文件是**补丁式参考实现**，需放回 `teach_agent` 的副本目录才能运行，
不是独立可安装的包。
