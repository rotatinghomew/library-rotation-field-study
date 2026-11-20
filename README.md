# 图书轮转试验的借阅增益与外溢

这里保存图书轮转试验的借阅增益与外溢工作所用的匿名合成记录。文件不包含分析结果或居民个人资料。

## 资料

`samples/` 中每个文件都是独立的 UTF-8 JSON 文档：`groups`、`households`、`copies`、`logs`、`requests`、`fulfillments`、`supply`、`commitments`、`floor`、`coverage`、`options`。日期是当地日历日期。空值表示没有对应观测或状态资料，不是数值零。

week 为周一。groups 登记试验安排与可能的邻馆代取；logs 是在架和馆方过程资料，已到手以 fulfillments 为准。copies.restricted 表示禁止跨馆调动；coverage 说明资料完整性。requests 中的 works 为必须同时满足的作品需要；accepted 只适用于对应的需要。supply 独立登记续期或待核的归还窗；实际借出和预订仍以馆方账为准。options 记录馆方来件里的副本安排。

## 运行

使用 Python 3.11，无需第三方依赖或服务。`python3 inspect_samples.py` 逐个读取资料并列出记录数；`python3 -m unittest -v` 检查关键原始记录间的关系。输出只描述原始资料，不是业务比较结果。
