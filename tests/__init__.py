"""测试包的包标记 —— 本文件的存在本身就是修复，不要删除。

【为什么必须有这个文件】
``test_fetchers_offline.py`` 里有一行::

    from tests.conftest import FakeResponse, FakeSession, ...

这行导入要求 ``tests`` 是一个**真正的包**。没有本文件时，``tests`` 只能
被当作「隐式命名空间包」（PEP 420），而命名空间包能否解析**取决于
运行环境把哪个目录放进了 sys.path**，这是一个不该被依赖的不确定条件。

实测后果：**本地 Windows 上能跑通，CI 的 ubuntu-latest 上直接收集失败**，
报错为「中断：收集错误」，pytest 退出码 2。因为四个 Python 版本一视同仁
地挂，看起来像代码问题，实际是导入机制问题。

【为什么当初没加，以及那个判断错在哪】
项目早期刻意不加本文件，理由是「加了会改变 pytest 的测试收集方式」。
这个顾虑本身没错（pytest 会从「rootdir 相对导入」切换到「包路径导入」），
但当时的判断漏掉了决定性的事实：**测试文件已经在用 ``tests.`` 这个包名了**。
既然代码里写了 ``from tests.conftest import``，就已经在要求 tests 是包，
不加 ``__init__.py`` 不是「避免改变收集方式」，而是**让这个导入依赖侥幸**。

同一个根因早期还以另一副面孔出现过：``mypy src tests`` 报
``Source file found twice under different module names: "conftest" and "tests.conftest"``。
当时的处置是把 mypy 范围缩到 ``src`` 规避。**那是规避，不是修复** ——
真正的问题是 ``tests`` 缺少包标记，导致同一个文件有两个可能的模块名。
加了本文件后该报错消失，验证了这一点。

【验证记录】
加本文件后本地 158 个测试全部通过，收集数量与收集方式均无变化。
"""
