# 图书轮转试验的借阅增益与外溢

这里保存图书轮转试验的借阅增益与外溢工作所用的匿名合成记录。文件不包含分析结果或居民个人资料。

## 资料

`samples/` 中每个文件都是独立的 UTF-8 JSON 文档：`groups`、`households`、`copies`、`logs`、`requests`、`fulfillments`、`supply`、`commitments`、`floor`、`coverage`、`options`。日期是当地日历日期。空值表示没有对应观测或状态资料，不是数值零。

week 为周一。groups 登记试验安排与可能的邻馆代取；logs 是在架和馆方过程资料，已到手以 fulfillments 为准。copies.restricted 表示禁止跨馆调动；coverage 说明资料完整性。requests 中的 works 为必须同时满足的作品需要；accepted 只适用于对应的需要。supply 独立登记续期或待核的归还窗；实际借出和预订仍以馆方账为准。options 记录馆方来件里的副本安排。

## 运行

使用 Python 3.11，无需第三方依赖或服务。`python3 inspect_samples.py` 逐个读取资料并列出记录数；`python3 -m unittest discover -s tests -v` 检查关键原始记录间的关系。输出只描述原始资料，不是业务比较结果。

## 整套阅读评估引擎（`rotation/`）

评估单位是**整套阅读请求**：`works` 必须同时满足，单独一册只算部分满足；截止前拿到一部分既不算整套完成，也不是从未服务。替代作品只有请求的 `accepted` 明确列出才可替；续借不新增一次满足。

- `rotation/data_model.py`：读取与跨表引用校验，统一以 ISO 周一为时间单位。
- `rotation/timeline.py`：按到架、预订、续期、待核归还窗与实际到手重建副本可用性。归还时刻不明时只输出 `earliest~latest` 边界，绝不在同一窗口把同一副本提前算给两家。
- `rotation/reconstruct.py`：以 `fulfillments` 馆方账为最高事实来源，还原每份请求的实际结果与两套归属（服务发生馆 / 机会所属馆）。
- `rotation/simulate.py`：**事实锚定**的反事实模拟。先回放馆方账的实际占用作为硬约束，已整套完成的请求原样锁定；只对未完成请求、在未被实际占用的副本上做条件性凑齐，输出乐观/保守区间。留架预订与候补经过未完整留痕，因此增量是库存可支撑的**可行性上限，不是分馆净增量**。
- `rotation/planner.py`：逐取件窗核对轮转组合对已确认承诺（读者预订、已签收轮转）的兑现、作品组覆盖相对分散留架基线是否跌破守护线、单册周转好但整套覆盖变差时阻止自动确认，并给出仅改一册的守底线候选；补录续借可查出承诺基础消失的排定组合，已签收家庭不再进入新一轮建议。

运行（建议用 `-I` 隔离模式，资料目录作为参数传入而非工作目录）：

```
python3 -I tools/run_analysis.py --data samples
python3 -I tools/run_analysis.py --data samples --renewal 副本09 2026-07-06 2026-07-13
python3 -I tools/run_tests.py
```

## 口径与边界

- 馆方账已证实的整套完成是事实；反事实数字标注为条件性上限，二者在输出中分列，不混为净增量。
- 资料不完整的馆-周（见 `coverage`）只给范围结论；`桥馆 2026-06-15` 即一例。
- 取书范围、截止日、可接受替代均固定在试验事先登记的请求上，比较不随供给办法改写。
- `restricted` 副本禁止跨馆调动；已确认承诺在模拟与轮转核对中均为硬约束。

