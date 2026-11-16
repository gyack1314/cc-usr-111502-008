# 跨业态明细仓的集合基数摘要与可组合估计

仓库为每个来源保存**有界集合摘要**，查询只读摘要与来源证明，不读原始身份。
小集合自动落到精确状态；大集合保留哈希阈值以下的完整样本，用二项反演给出
有依据的规模区间。并、交、差都在共同阈值以下逐值计算，不会把几个去重计数
相加减来充当交集。

## 组件

- `identity.py`：固定的有类型身份哈希（null 不计入人数）。
- `summary.py`：`Builder` / `Summary` 有界摘要。状态序列化后可在另一进程恢复，
  含义不变；哈希种子或版本不同会拒绝组合，精度不同按策略拒绝或兼容降阶。
- `fragments.py`：`Fragments` 入仓片段清单、完成回执与内容指纹。重复发送且
  内容相同是幂等的；内容不同记为冲突；缺失或冲突都不能宣称完成。
- `expression.py`：`leaf` / `union` / `intersection` / `difference` 有序表达式树、
  求值器与 `result_leaf`（把结果摘要继续用于组合）。
- `warehouse.py`：`Warehouse` 仓内聚合状态，管理来源登记、片段接收、定稿、
  查询、`applicability()` 依据说明以及 `export()` / `restore()` 跨进程恢复。
- `budget.py`：查询级共享存储预算，覆盖叶摘要、合并临时态与接收缓冲。
- `exact.py`：原小集合精确表示，保留了旧的 `exact-1` 状态兼容读取。

## 摘要语义

| `threshold_kind` | 含义 |
| --- | --- |
| `exhaustive` | 阈值是完整哈希域，`values` 是全集，规模精确。 |
| `topk` | 保留最小的 `precision` 个哈希，阈值是第 `precision + 1` 个（属于集合）。 |
| `common` | 交、差与退化并集的结果：共同边界以下完整，阈值本身不一定属于集合。 |

近似区间有两种方法：`order-statistic-binomial-inversion`（阈值是次序统计量）与
`common-threshold-binomial-inversion`（阈值是共同取样边界）。每侧尾部取
`(1 - confidence) / 4`，为阈值本身的随机性留出余量。一个样本都没观测到时，
区间是 `[0, upper]` 并显式标记 `zero_is_not_certain: true`；只有精确空交集才
给出确定的零。

## 运行

运行环境为 Python 3.11，只用标准库，数据位于 `data/sample.json`。

```sh
python3 -m setwarehouse
python3 -m unittest discover -s tests -v
python3 -m compileall -q setwarehouse tests
```

`__main__.py` 会输出：分片回执、示例并/交/差查询、`applicability()` 依据、
序列化状态大小与“不含原始身份”的核对，以及一组合成大集合的估计与区间。

文件职责与数据约定见 `contracts.md`。测试使用临时内存状态，不改写项目数据。
