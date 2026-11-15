# 跨业态明细仓的集合基数摘要与可组合估计

identity.py 提供固定的有类型身份哈希，fragments.py 定义分片完成清单与内容指纹，exact.py 是目前仅适合小集合的精确表示。当前没有近似摘要或集合表达式执行器。

运行环境为 Python 3.11。代码只使用标准库，数据位于 data/sample.json。
包内的 __main__.py 运行现有组件并输出其实际处理结果。

```sh
python3 -m setwarehouse
python3 -m unittest discover -s tests -v
python3 -m compileall -q setwarehouse tests
```

文件职责与数据约定见 contracts.md。测试使用临时目录，不改写项目内的数据。
