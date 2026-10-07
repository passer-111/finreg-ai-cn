"""命令行入口 —— 通过 ``finreg`` 命令使用本项目。

提供七组命令：

===============  ====================================================
命令              用途
===============  ====================================================
``fetch``         执行抓取流水线，发现新政策并产出变更流
``validate``      校验仓库全部数据的完整性（CI 使用）
``validate-sources``  只校验数据源登记表
``list``          列出政策记录，支持按状态、机构、相关度筛选
``show``          查看单条政策记录的完整信息
``stale``         列出超期未核验的记录（时效性治理的核心工具）
``build-site``    从 data/ 生成静态站点（对外展示的只读视图）
===============  ====================================================

设计上使用标准库 ``argparse`` 而非 click 等第三方库。
理由：本项目定位是「三年后依然能跑起来」的长期基础设施，
每减少一个第三方依赖，就少一个未来破坏兼容性的风险点。
"""

# 导入 argparse 用于构建命令行界面
import argparse
# 导入 json 用于输出 JSON 格式
import json
# 导入 sys 用于设置退出码
import sys
# 导入 date 类型：stale 的 --as-of 参数需要接收并返回日期对象
from datetime import date
# 导入 Sequence 类型标注。
# 注意这里从 collections.abc 而非 typing 导入：Python 3.9 起
# typing.Sequence 等别名已被标记为弃用，泛型容器类型应直接使用标准库抽象基类。
from collections.abc import Sequence
# 导入 Path：build-site 的 --output 需要接收路径
from pathlib import Path

# 导入包版本号
from finreg_ai import __version__
# 从抓取器包导入日期工具
from finreg_ai.fetchers.base import (
    DROP_REASON_EXCLUDED,       # 关键词丢弃原因：命中排除词
    DROP_REASON_NOT_MATCHED,    # 关键词丢弃原因：未命中包含词
    FetchResult,                # 单个源的抓取结果，供明细渲染函数的类型标注使用
    today_china,                # 当前北京日期
)
# 导入政策对象的字典化函数，用于 show --json 输出。
# 早期这里不在模块顶部导入，而是在一个包装函数内部做局部 import，
# 那个包装函数只是原样转调 policy_to_dict，没有任何附加逻辑——
# 属于「为了一个已经不成立的顾虑而多写一层」。现在直接导入使用。
from finreg_ai.models import policy_to_dict
# 导入流水线函数
from finreg_ai.pipeline import run_pipeline, validate_repository
# 导入静态站生成器
from finreg_ai.site import build_site
# 导入数据加载函数
from finreg_ai.store import load_all_policies, load_sources
# 导入核验取证层：草稿加载、出证、终端渲染
from finreg_ai.verify import build_all_cards, load_drafts, render_card_lines

# 退出码约定：0 成功，1 发现问题（校验失败），2 用法错误（argparse 默认）
EXIT_OK = 0       # 一切正常
EXIT_ISSUES = 1   # 发现数据问题


def _parse_iso_date_arg(value: str) -> date:
    """把 ``YYYY-MM-DD`` 形式的字符串解析为 ``date``，供 argparse 的 type 使用。

    单独抽成模块级函数而非写 lambda 的原因：
    argparse 的 ``type`` 收到的是原始字符串，若格式非法需要抛
    ``ValueError`` 或 ``argparse.ArgumentTypeError``。
    抛后者能把错误信息渲染成「用法错误」并返回退出码 2（而不是 1），
    这样「用户敲错了参数格式」与「数据本身有问题」在退出码上可区分——
    对 CI 而言这两者的处置方式完全不同。

    显式写 rsplit 而非交给 ``date.fromisoformat``：后者对
    ``2026-1-5``（月份未补零）等写法会接受，而我们希望格式严格统一，
    因为该值可能被复制进 issue 正文与日志，格式漂移会让检索变困难。
    """
    # 严格按三段切分，不允许多余部分
    parts = value.split("-")
    # 段数必须为 3，否则说明不是 YYYY-MM-DD
    if len(parts) != 3:
        # 抛出 argparse 的用法错误，退出码为 2
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD，收到：{value!r}")
    # 年月日三段都必须是纯数字且长度正确
    year, month, day = parts
    # 逐段校验长度，拒绝 2026-1-5 这类未补零写法
    if len(year) != 4 or len(month) != 2 or len(day) != 2:
        # 抛出用法错误
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD（需补零），收到：{value!r}")
    # 交给标准库做真实日期校验（会拦住 2026-02-30 这类不存在的日期）
    try:
        # 三段均为数字时才能构造，否则抛 ValueError
        return date(int(year), int(month), int(day))
    except ValueError:
        # 转换为用法错误，保持退出码语义一致
        raise argparse.ArgumentTypeError(f"不是合法的日历日期：{value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    # 创建顶层解析器
    parser = argparse.ArgumentParser(
        prog="finreg",                                                  # 程序名
        description="中国金融领域 AI 合规政策知识库 —— 抓取、校验与检索工具",  # 描述
    )
    # 添加版本参数
    parser.add_argument("--version", action="version", version=f"finreg-ai-cn {__version__}")

    # 创建子命令容器
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ------------------------------------------------------------
    # fetch：执行抓取流水线
    # ------------------------------------------------------------
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="执行抓取流水线，发现新政策并写入变更流",
    )
    # 允许只抓指定源，用于手动重试失败的源
    fetch_parser.add_argument(
        "--source",                     # 参数名
        action="append",                # 可重复出现，累积成列表
        dest="sources",                 # 存放的目标属性名
        metavar="SOURCE_ID",            # 帮助信息中的占位符
        help="只抓取指定的数据源 id，可重复指定。不指定时抓取全部启用的源",  # 说明
    )
    # 允许只输出报告不落盘，用于试跑
    fetch_parser.add_argument(
        "--dry-run",                    # 参数名
        action="store_true",            # 布尔开关
        help="只输出报告，不写入变更流文件",  # 说明
    )
    # 允许输出 JSON 以便程序消费
    fetch_parser.add_argument(
        "--json",                       # 参数名
        action="store_true",            # 布尔开关
        help="以 JSON 格式输出报告",      # 说明
    )
    # 列出被关键词过滤丢弃的条目标题
    fetch_parser.add_argument(
        "--show-dropped",               # 参数名
        action="store_true",            # 布尔开关
        help="列出被关键词过滤丢弃的条目标题，用于复核「阈值是否定得太窄」",  # 说明
    )
    # 允许运行已停用的源
    fetch_parser.add_argument(
        "--force",                      # 参数名
        action="store_true",            # 布尔开关
        help="连 enabled: false 的源一并抓取。用于按需拉取「刻意不放进每日流水线」的源"
             "（如行政处罚公示——它每次产出数百条，进了变更流会淹没真正的政策变化）",  # 说明
    )
    # 记录级变更检测的基准目录
    fetch_parser.add_argument(
        "--baseline",                   # 参数名
        type=Path,                      # 转成 Path
        default=None,                   # 默认不启用记录级检测
        metavar="DIR",                  # 占位符
        help="以指定目录中的政策记录作为旧版本，检测记录级变更（新增/状态/内容/陈旧）。"
             "CI 在抓取前从 git 历史导出后传入；不指定时跳过记录级检测",  # 说明
    )

    # ------------------------------------------------------------
    # validate：校验数据完整性
    # ------------------------------------------------------------
    validate_parser = subparsers.add_parser(
        "validate",
        help="校验仓库全部数据的完整性（CI 使用）",
    )
    # 陈旧阈值，超过该天数未核验会告警
    validate_parser.add_argument(
        "--stale-days",                 # 参数名
        type=int,                       # 整数类型
        default=180,                    # 默认 180 天
        metavar="N",                    # 占位符
        help="超过 N 天未核验的记录视为陈旧，默认 180",  # 说明
    )
    # 严格模式：出现陈旧记录也返回非零退出码
    validate_parser.add_argument(
        "--strict-stale",               # 参数名
        action="store_true",            # 布尔开关
        help="将陈旧记录也视为失败（默认只告警）",  # 说明
    )

    # ------------------------------------------------------------
    # validate-sources：只校验数据源登记表
    # ------------------------------------------------------------
    subparsers.add_parser(
        "validate-sources",
        help="只校验数据源登记表 data/sources.yaml",
    )

    # ------------------------------------------------------------
    # list：列出政策记录
    # ------------------------------------------------------------
    list_parser = subparsers.add_parser(
        "list",
        help="列出政策记录，支持筛选",
    )
    # 按状态筛选
    list_parser.add_argument(
        "--status",                     # 参数名
        action="append",                # 可重复
        dest="statuses",                # 目标属性
        metavar="STATUS",               # 占位符
        help="按法律状态筛选，如 effective / consultation，可重复指定",  # 说明
    )
    # 只显示现行有效的
    list_parser.add_argument(
        "--effective-only",             # 参数名
        action="store_true",            # 布尔开关
        help="只显示当前有效（effective / partially_effective / amended）的记录",  # 说明
    )
    # 按发布机构筛选
    list_parser.add_argument(
        "--issuer",                     # 参数名
        metavar="CODE",                 # 占位符
        help="按机构代码筛选，如 nfra / cac / pbc",  # 说明
    )
    # 按 AI 相关度筛选
    list_parser.add_argument(
        "--ai-relevance",               # 参数名
        choices=["core", "related", "background"],  # 限定取值
        metavar="LEVEL",                # 占位符
        help="按 AI 相关度筛选：core / related / background",  # 说明
    )
    # 按主题标签筛选
    list_parser.add_argument(
        "--topic",                      # 参数名
        action="append",                # 可重复
        dest="topics",                  # 目标属性
        metavar="TOPIC",                # 占位符
        help="按主题标签筛选，可重复指定",  # 说明
    )
    # 输出格式
    list_parser.add_argument(
        "--json",                       # 参数名
        action="store_true",            # 布尔开关
        help="以 JSON 格式输出",          # 说明
    )

    # ------------------------------------------------------------
    # show：查看单条记录
    # ------------------------------------------------------------
    show_parser = subparsers.add_parser(
        "show",
        help="查看单条政策记录的完整信息",
    )
    # 政策 id 为位置参数
    show_parser.add_argument(
        "policy_id",                    # 参数名
        metavar="POLICY_ID",            # 占位符
        help="政策记录 id，如 nfra-2026-ai-guidance",  # 说明
    )
    # 输出格式
    show_parser.add_argument(
        "--json",                       # 参数名
        action="store_true",            # 布尔开关
        help="以 JSON 格式输出",          # 说明
    )

    # ------------------------------------------------------------
    # stale：列出陈旧记录
    # ------------------------------------------------------------
    stale_parser = subparsers.add_parser(
        "stale",
        help="列出超期未核验的记录（时效性治理工具）",
    )
    # 陈旧阈值
    stale_parser.add_argument(
        "--days",                       # 参数名
        type=int,                       # 整数
        default=180,                    # 默认 180 天
        metavar="N",                    # 占位符
        help="超过 N 天未核验视为陈旧，默认 180",  # 说明
    )
    # 【为什么需要这个参数】判定「陈旧」必须有一个基准日。
    # 默认取北京时间今天，但把基准日做成可传入的，有两个必要理由：
    #   1. **测试必须能注入固定日期**。否则测试结果随运行日期漂移——
    #      今天跑是绿的，过两天同一条断言就红了，而代码一行没改。
    #      本项目已经因此吃过一次亏：一条断言依赖「记录核验日期是今天」，
    #      核验台账一变成两天前，CI 就无端变红。
    #   2. **运维需要预演**。想提前知道「再过 60 天哪些记录会进入待核验队列」，
    #      直接 `--as-of` 未来日期即可，不必等到真到那天。
    stale_parser.add_argument(
        "--as-of",                      # 参数名
        type=_parse_iso_date_arg,       # 解析为 date，非法输入由 argparse 报错
        default=None,                   # 默认 None 表示「取北京时间今天」
        metavar="YYYY-MM-DD",           # 占位符，明确告知格式
        help="以指定日期为基准计算未核验天数，默认取北京时间的今天",  # 说明
    )

    # ------------------------------------------------------------
    # verify：本地核验 —— 对 data/drafts/ 的草稿出证据卡
    # ------------------------------------------------------------
    verify_parser = subparsers.add_parser(
        "verify",
        help="对 data/drafts/ 中的草稿做机器取证，产出核验证据卡",
    )
    # 指定单条草稿 id；不指定时需配合 --all
    verify_parser.add_argument(
        "policy_id",                    # 参数名
        nargs="?",                      # 可选位置参数（--all 时省略）
        default=None,                   # 默认不指定
        metavar="POLICY_ID",            # 占位符
        help="要取证的草稿 id，如 npc-2021-pipl；与 --all 二选一",  # 说明
    )
    # 对全部草稿出证
    verify_parser.add_argument(
        "--all",                        # 参数名
        action="store_true",            # 布尔开关
        help="对 data/drafts/ 中全部草稿出证",  # 说明
    )
    # 输出 JSON 以便程序消费（核验台界面也复用同一结构）
    verify_parser.add_argument(
        "--json",                       # 参数名
        action="store_true",            # 布尔开关
        help="以 JSON 格式输出证据卡",     # 说明
    )
    # 把证据卡写成 JSON 文件（可选；默认只打印不落盘）
    verify_parser.add_argument(
        "--output-dir",                 # 参数名
        type=Path,                      # 转成 Path
        default=None,                   # 默认不落盘
        metavar="DIR",                  # 占位符
        help="把证据卡 JSON 写入指定目录（每张卡一个文件）",  # 说明
    )

    # ------------------------------------------------------------
    # build-site：生成静态站点
    # ------------------------------------------------------------
    site_parser = subparsers.add_parser(
        "build-site",
        help="从 data/ 生成静态站点（对外展示的只读视图）",
    )
    # 输出目录。做成可配置而非写死，有两个必要理由：
    #   1. CI 里需要输出到临时目录再作为发布产物上传，不该污染工作区；
    #   2. 本地预览时可能想生成到一个随手可删的位置。
    site_parser.add_argument(
        "--output",                     # 参数名
        type=Path,                      # argparse 直接转成 Path
        default=None,                   # 默认 None 表示使用 site.DEFAULT_OUTPUT_DIR
        metavar="DIR",                  # 占位符
        help="输出目录，默认 docs/",      # 说明
    )
    # 输出 JSON 以便程序消费（CI 里用来判断构建结果）
    site_parser.add_argument(
        "--json",                       # 参数名
        action="store_true",            # 布尔开关
        help="以 JSON 格式输出构建结果",   # 说明
    )

    # 返回解析器
    return parser


# ============================================================
# 各子命令实现
# ============================================================

# 关键词丢弃原因的中文说明，把内部原因码翻译成人能读懂的话。
# 之所以要区分两种原因：它们的处置方向完全相反——
# 「命中排除词」多说明排除词写得过宽（误伤真实政策），
# 「未命中包含词」多说明包含词写得太窄（漏检 AI 相关文件）。
_FILTER_DROP_LABELS = {
    DROP_REASON_EXCLUDED: "命中排除词",       # 被 exclude_keywords 排除
    DROP_REASON_NOT_MATCHED: "未命中包含词",   # 不含任何 include_keywords
}


def render_source_detail_lines(result: FetchResult) -> list[str]:
    """把一个抓取结果的「处理过程明细」渲染成带树形连接符的文本行。

    为什么要把这段从 ``cmd_fetch`` 里抽出来
    --------------------------------------
    两个原因，都不是为了好看：

    1. **明细行数不固定**。导航剔除、关键词丢弃、错误说明可能同时出现，
       也可能一个都没有；而树形连接符要求「只有最后一行用 └─」。
       内联写法（每发现一项就 print 一次）在增删明细项时极易出现
       两个 └─ 或一个都没有——本项目已经踩过一次。抽成函数后，
       「先收集、后渲染」的顺序被结构固定住，不再依赖写代码时的注意力。
    2. **可离线测试**。内联在命令函数里的输出只能通过让整条流水线
       产出特定的 FetchResult 才能覆盖，而真实的 FetchResult 要联网。
       抽成纯函数后，可以构造任意组合直接断言渲染结果。

    明细的**内容**与**顺序**都是刻意的：
    - 先「被剔除的导航链接」，再「被关键词滤除的条目」，最后「错误」。
      即按「从正常处理流程到异常」排列，让读者先看到常规的清洗动作。
    - 每一项都是「数量 + 原因」而非只给数量。只给数字无法判断
      该采取什么行动：丢弃多到底是排除词太宽还是包含词太窄？
    """
    # 待打印的明细文本（尚未加连接符）
    details: list[str] = []

    # 剔除的导航链接数。这一项必须显示：清洗只要不可见就会变成
    # 新的静默风险——某天一次改版让大量真实条目被误判为导航，
    # 而报告依旧显示 ok，没有人会发现。
    if result.dropped_navigation:
        # 记录剔除数量
        details.append(f"已剔除 {result.dropped_navigation} 条疑似栏目导航链接")

    # 关键词过滤丢弃数。与导航剔除同属静默风险：标题不含 AI 字样、
    # 正文却含 AI 条款的文件会被滤掉，若不显示则完全无迹可查。
    if result.dropped_by_filter:
        # 把原因码翻译成中文，并按原因名排序保证输出稳定
        # （字典序在 Python 中是确定的，因此同一份数据每次输出一致，
        #   测试才能断言具体文本，而不会随哈希种子漂移）
        reason_text = "、".join(
            f"{_FILTER_DROP_LABELS.get(reason, reason)} {count} 条"
            for reason, count in sorted(result.filter_drop_reasons.items())
        )
        # 组装说明
        detail = f"已滤除 {result.dropped_by_filter} 条未进入结果的关键词过滤丢弃"
        # 仅有明细时补充原因细分
        if reason_text:
            # 附上原因细分
            detail += f"（{reason_text}）"
        # 收集
        details.append(detail)

    # 错误说明。放在最后：它是异常，不是常规清洗动作。
    if result.error:
        # 收集
        details.append(result.error)

    # 渲染：最后一行用 └─ 收尾，其余用 ├─
    lines: list[str] = []
    # 逐行加连接符与缩进
    for index, text in enumerate(details):
        # 判断是否为最后一行
        branch = "└─" if index == len(details) - 1 else "├─"
        # 缩进对齐到源名下方
        lines.append(f"           {branch} {text}")
    # 返回渲染结果
    return lines


# 单次 --show-dropped 输出中，单个源最多列出的丢弃条目数。
#
# 为什么要设上限：实测 nfra-regulations 一次丢弃 151 条，
# 无上限地把它们全打出来会淹没同一次输出里的其他源——
# 而其他源正因为「丢弃少」才更需要被看见。
# 截断时必须明确写出「还有 N 条未列出」，绝不能不声不响地少打几行：
# 那正是本项目一直在对抗的「静默」。
DROPPED_DOCS_LIMIT = 40


def render_dropped_docs(result: FetchResult, limit: int = DROPPED_DOCS_LIMIT) -> list[str]:
    """把被关键词过滤丢弃的条目渲染成可打印的文本行。

    为什么这件事必须能做
    --------------------
    ``已滤除 151 条`` 是一个**无法行动**的数字。维护者看到它，既不知道
    被丢的是《银行业保险业数字金融高质量发展实施方案》还是某场表彰大会的
    新闻稿，也就无从判断该不该改关键词。要复核「阈值是不是定得太窄」，
    唯一的办法就是能把被丢弃的标题拿出来逐条看。

    本函数只做渲染，不做判断——它不猜哪些「看起来重要」。
    原因：一旦在这里加入自己的相关性判断，就成了第二个关键词过滤器，
    而它的判断依据会比配置里的关键词更不可见。保持它「如实呈现」。
    """
    # 没有任何丢弃时输出为空，不打印标题行
    if not result.dropped_filter_docs:
        # 返回空列表
        return []

    # 输出行
    lines: list[str] = []
    # 标题行：说明这是复核用途，并给出总数
    lines.append(f"           ┄ 以下为被滤除的条目（共 {len(result.dropped_filter_docs)} 条，供复核）：")
    # 取前 limit 条
    shown = result.dropped_filter_docs[:limit]
    # 逐条输出
    for doc in shown:
        # 日期缺失时用占位符，避免输出 None
        date_text = doc.published_on.isoformat() if doc.published_on else "无日期"
        # 组装一行
        lines.append(f"             · [{date_text}] {doc.title}")
    # 被截断时明确说明还剩多少条，绝不静默少打
    if len(result.dropped_filter_docs) > limit:
        # 计算剩余数量
        remaining = len(result.dropped_filter_docs) - limit
        # 追加说明
        lines.append(f"             …… 另有 {remaining} 条未列出（上限 {limit} 条）")
    # 返回渲染结果
    return lines


def cmd_fetch(args: argparse.Namespace) -> int:
    """执行 fetch 子命令。"""
    # 执行流水线
    report = run_pipeline(
        source_ids=args.sources,            # 指定的源列表（可能为 None）
        write_changes=not args.dry_run,     # dry-run 时不写文件
        force=args.force,                   # --force 时连停用的源一并抓
        baseline_dir=args.baseline,         # --baseline 提供时启用记录级变更检测
    )

    # JSON 输出模式
    if args.json:
        # 直接打印结构化报告
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        # 人类可读输出
        print(f"抓取完成 —— {report.run_on.isoformat()}")
        print()
        # 打印各源结果
        print("各源结果：")
        # 逐源输出
        for result in report.fetch_results:
            # 状态标记：用文字而非符号，避免 Windows 控制台编码问题
            marker = {"ok": "[OK]", "empty": "[空]", "degraded": "[降级]", "failed": "[失败]", "skipped": "[跳过]"}.get(
                result.status, "[未知]"
            )
            # 输出一行
            print(f"  {marker:<8} {result.source_id:<24} 条目数={len(result.docs)}")

            # 输出本源的处理过程明细（导航剔除 / 关键词丢弃 / 错误说明）。
            # 明细的组装与树形连接符的渲染都在 render_source_detail_lines 里，
            # 那里说明了「为什么先收集再打印」以及为什么这段必须是可测的纯函数。
            for line in render_source_detail_lines(result):
                # 逐行输出
                print(line)

            # 需要复核丢弃内容时，把被滤掉的标题逐条列出来。
            # 默认不列：151 条标题会把报告淹没，日常抓取只需看到计数；
            # 但一旦决定复核「关键词是不是定窄了」，就必须能看到具体丢了什么。
            if args.show_dropped:
                # 渲染并逐行输出
                for line in render_dropped_docs(result):
                    # 输出
                    print(line)

        # 打印概要
        print()
        print(f"发现条目合计：{report.total_docs}")
        # 变更集合存在时打印变更统计
        if report.change_set:
            # 按变更类型分组统计
            counts: dict[str, int] = {}
            # 遍历变更
            for change in report.change_set.changes:
                # 累加各类型计数
                counts[change.change_type] = counts.get(change.change_type, 0) + 1
            # 输出统计
            print(f"变更条数：{len(report.change_set.changes)}  {counts if counts else ''}")

        # 打印变更文件的写入结果。合并这件事必须可见——
        # 若某次运行往文件里多带了「不是本次抓到的」条目而没人说，
        # 使用者会把它们当成今天的发现。
        if report.change_write:
            # 取出写入结果
            outcome = report.change_write
            # 打印落盘位置与合并情况
            print(f"变更文件：{outcome.path.name}  共 {outcome.total} 条"
                  f"（本次新增 {outcome.added}、覆盖 {outcome.replaced}）")
            # 保留了旧条目时单独说一句，否则「总数大于本次产出」无法解释
            if outcome.kept_from_previous:
                # 说明保留数及原因
                print(f"          保留文件中已有的 {outcome.kept_from_previous} 条"
                      f"（本次未产出，通常是未参与本次运行的源）")
            # 旧文件读不了时明确报出，避免损坏被下一次覆盖悄悄抹掉
            if outcome.previous_unreadable:
                # 打印警告
                print(f"          警告：既有变更文件无法合并，已整份重写 —— {outcome.previous_unreadable}")

        # 打印失败源——这一项必须显著，否则「无变更」会被误读为「一切正常」
        if report.failed_sources:
            # 打印警告
            print()
            print(f"警告：{len(report.failed_sources)} 个源未成功抓取：{', '.join(report.failed_sources)}")
            # 提示人工确认
            print("     这些源的覆盖存在缺口，请人工确认后再判断「无新政策」")

        # 打印致命错误
        if report.fatal_errors:
            # 逐条输出
            print()
            print("致命错误：")
            # 遍历打印
            for err in report.fatal_errors:
                # 输出
                print(f"  - {err}")
            # 返回失败码
            return EXIT_ISSUES

    # 成功返回
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    """执行 validate 子命令。"""
    # 执行校验，拿到错误、警告、陈旧记录三份清单
    errors, warnings, stale = validate_repository(stale_threshold_days=args.stale_days)

    # 输出错误清单——错误必须阻断，因此单独成段并置顶
    if errors:
        # 打印标题
        print(f"发现 {len(errors)} 个数据错误（必须修复）：")
        # 逐条输出
        for problem in errors:
            # 打印问题
            print(f"  - {problem}")

    # 输出警告清单——不阻断，但需要提示
    if warnings:
        # 与错误之间留空行，视觉上区分严重级别
        if errors:
            # 分隔
            print()
        # 打印标题
        print(f"发现 {len(warnings)} 个待办提示（不阻断）：")
        # 逐条输出
        for warning in warnings:
            # 打印提示
            print(f"  - {warning}")

    # 输出陈旧清单
    if stale:
        # 打印标题
        print()
        print(f"发现 {len(stale)} 条陈旧记录（超过 {args.stale_days} 天未核验）：")
        # 逐条输出
        for pid, days in stale:
            # 打印记录与天数
            print(f"  - {pid}：{days} 天")
        # 提示更新方式
        print()
        print("处理方式：打开官方页面核对状态，更新 last_verified 与 status 字段。")

    # 全无问题时给出明确通过信息
    if not errors and not warnings and not stale:
        # 打印通过
        print("数据校验通过，未发现任何问题。")

    # 只有错误才导致退出码非零；警告与陈旧记录不阻断（除非指定严格模式）
    if errors:
        # 返回失败码
        return EXIT_ISSUES
    # 严格模式且存在陈旧记录时失败
    if stale and args.strict_stale:
        # 返回失败码
        return EXIT_ISSUES
    # 通过
    return EXIT_OK


def cmd_validate_sources(_args: argparse.Namespace) -> int:
    """执行 validate-sources 子命令。"""
    # 加载数据源登记表
    issuers, sources, errors = load_sources()
    # 有错误时输出并返回失败码
    if errors:
        # 打印错误数
        print(f"数据源登记表存在 {len(errors)} 个问题：")
        # 逐条输出
        for err in errors:
            # 打印
            print(f"  - {err}")
        # 返回失败码
        return EXIT_ISSUES

    # 统计启用情况
    enabled = [s for s in sources.values() if s.enabled and s.fetcher != "manual"]
    # 人工录入源
    manual = [s for s in sources.values() if s.fetcher == "manual"]
    # 输出统计
    print(f"数据源登记表校验通过：{len(issuers)} 个机构，{len(sources)} 个数据源")
    # 输出启用统计
    print(f"  自动抓取：{len(enabled)} 个   人工录入：{len(manual)} 个   未启用：{len(sources) - len(enabled) - len(manual)} 个")
    # 通过
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    """执行 list 子命令。"""
    # 加载全部政策
    policies, errors = load_all_policies()
    # 有问题时提示但不中断，因为可能只是想看现有数据
    if errors:
        # 输出到 stderr，避免污染正常输出
        print(f"提示：加载过程中发现 {len(errors)} 个问题，运行 finreg validate 查看详情", file=sys.stderr)

    # 逐条筛选
    selected = list(policies.values())
    # 状态筛选
    if args.statuses:
        # 转成集合加速判断
        wanted = set(args.statuses)
        # 过滤
        selected = [p for p in selected if p.status.value in wanted]
    # 现行有效筛选
    if args.effective_only:
        # 使用模型提供的判断属性
        selected = [p for p in selected if p.status.is_currently_valid]
    # 机构筛选
    if args.issuer:
        # 按机构代码匹配
        selected = [p for p in selected if p.issuer_code == args.issuer]
    # AI 相关度筛选
    if args.ai_relevance:
        # 按相关度匹配
        selected = [p for p in selected if p.ai_relevance.value == args.ai_relevance]
    # 主题标签筛选：只要命中任一指定主题即保留
    if args.topics:
        # 转成集合
        wanted_topics = set(args.topics)
        # 过滤
        selected = [p for p in selected if wanted_topics & set(p.topics)]

    # 按公布日期降序排列，最新政策排在最前
    selected.sort(key=lambda p: p.published_on, reverse=True)

    # JSON 输出模式
    if args.json:
        # 输出精简字段的 JSON
        print(
            json.dumps(
                [
                    {
                        "id": p.id,                                     # 标识
                        "title": p.title,                               # 标题
                        "issuer": p.issuer,                             # 机构
                        "status": p.status.value,                       # 状态
                        "effective_from": p.effective_from.isoformat() if p.effective_from else None,  # 生效日
                        "ai_relevance": p.ai_relevance.value,           # AI 相关度
                        "topics": p.topics,                             # 主题
                        "url": p.source.url,                            # 官方链接
                    }
                    for p in selected
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        # 返回成功
        return EXIT_OK

    # 人类可读输出
    if not selected:
        # 无结果时给出提示
        print("没有符合条件的记录。")
        # 返回成功（空结果不是错误）
        return EXIT_OK

    # 打印表头
    print(f"共 {len(selected)} 条记录：")
    print()
    # 逐条输出，用固定宽度对齐
    for policy in selected:
        # 计算生效日期一栏的显示文本。
        # 注意：「有生效日期」「未生效」「日期待核验」是三种不同的状态，
        # 不能一律显示为「未生效」——那会让一条现行有效的政策看起来还没生效，
        # 是会被误读的严重显示错误。
        if policy.effective_from is not None:
            # 正常情况：有明确生效日期
            eff = policy.effective_from.isoformat()
        elif policy.status.is_currently_valid:
            # 现行有效但日期缺失：说明施行日期尚未核验出来，显式标注
            eff = "日期待核验"
        else:
            # 确实尚未生效（草稿、征求意见、已公布未生效）
            eff = "未生效"
        # 输出格式化行
        print(f"  {policy.id:<38} {policy.status.value:<20} {eff:<12} {policy.title[:40]}")
    # 返回成功
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    """执行 show 子命令。"""
    # 加载全部政策
    policies, _errors = load_all_policies()
    # 查找指定记录
    policy = policies.get(args.policy_id)
    # 未找到时输出提示
    if policy is None:
        # 打印错误到 stderr
        print(f"未找到政策记录：{args.policy_id}", file=sys.stderr)
        # 提示可用 id
        print(f"当前库中共有 {len(policies)} 条记录，可运行 finreg list 查看", file=sys.stderr)
        # 返回失败码
        return EXIT_ISSUES

    # JSON 输出模式
    if args.json:
        # 输出结构化数据
        print(json.dumps(policy_to_dict(policy), ensure_ascii=False, indent=2))
        # 返回成功
        return EXIT_OK

    # 人类可读输出
    print(f"标题：{policy.title}")
    # 官方全称之后打印标识与机构
    print(f"标识：{policy.id}")
    # 发布机构
    print(f"机构：{policy.issuer}")
    # 文件层级与约束力
    print(f"层级：{policy.instrument_type.value}    约束力：{policy.bindingness.value}")
    # 法律状态（重点字段，用醒目格式）
    print(f"状态：{policy.status.value}")
    # 日期信息
    print(f"公布日期：{policy.published_on.isoformat()}")
    # 生效日期——同样区分「未生效」与「日期待核验」两种情况
    if policy.effective_from is not None:
        # 有明确日期
        print(f"生效日期：{policy.effective_from.isoformat()}")
    elif policy.status.is_currently_valid:
        # 现行有效但日期未核验，显式提示需补充
        print("生效日期：（待核验 —— 该政策现行有效，但施行日期尚未确认）")
    else:
        # 尚未生效
        print("生效日期：（尚未生效）")
    # 失效日期
    print(f"失效日期：{policy.effective_until.isoformat() if policy.effective_until else '（无固定期限）'}")
    # 版本链
    print(f"版本：{len(policy.versions)} 个")
    # 当前有效与否的明确结论
    print(f"现行有效：{'是' if policy.status.is_currently_valid else '否'}")
    print()
    # 官方链接
    print(f"官方链接：{policy.source.url}")
    # 核验信息
    print(f"最近核验：{policy.last_verified.isoformat()}（{policy.verified_by.value}）")
    # 适用主体
    if policy.applicable_to:
        # 输出适用主体
        print(f"适用主体：{'、'.join(policy.applicable_to)}")
    # 关键义务
    if policy.key_obligations:
        # 打印标题
        print()
        # 打印义务数
        print(f"关键义务（{len(policy.key_obligations)} 条）：")
        # 逐条输出
        for obligation in policy.key_obligations:
            # 打印条款与概括
            print(f"  {obligation.clause}. {obligation.summary}")
    # 返回成功
    return EXIT_OK


def cmd_stale(args: argparse.Namespace) -> int:
    """执行 stale 子命令。"""
    # 加载全部政策
    policies, _errors = load_all_policies()
    # 确定基准日。
    # 【这一行是防测试漂移的关键】若用户显式传了 --as-of，就用它；
    # 否则取北京时间今天。把基准日变成「可注入」而非在函数内部隐式取当天，
    # 是为了让测试能钉死日期——否则同一条断言会在核验台账变旧后突然变红，
    # 而代码一行没改，排查时极易误判为逻辑坏了。
    ref_day = args.as_of or today_china()
    # 筛选陈旧记录
    stale = []
    # 逐条计算
    for pid, policy in policies.items():
        # 计算距上次核验天数
        days = policy.days_since_verified(ref_day)
        # 超过阈值则记录
        if days > args.days:
            # 追加 (id, 天数, 记录) 三元组
            stale.append((pid, days, policy))
    # 按天数降序排列
    stale.sort(key=lambda item: item[1], reverse=True)

    # 无陈旧记录时输出提示
    if not stale:
        # 打印结果
        print(f"没有超过 {args.days} 天未核验的记录。")
        # 返回成功
        return EXIT_OK

    # 打印标题。
    # 用了 --as-of 时把基准日一并写进标题：否则用户看到的天数与「今天」对不上，
    # 会怀疑工具算错了。写明基准日，输出即自解释。
    if args.as_of is not None:
        # 显式标注基准日
        print(f"共 {len(stale)} 条记录超过 {args.days} 天未核验（基准日 {ref_day.isoformat()}）：")
    else:
        # 未指定基准日时保持原有输出格式
        print(f"共 {len(stale)} 条记录超过 {args.days} 天未核验：")
    print()
    # 逐条输出
    for pid, days, policy in stale:
        # 输出记录、天数与状态
        print(f"  {pid:<38} {days:>5} 天    状态={policy.status.value}")
        # 输出官方链接便于直接核对
        print(f"    └─ {policy.source.url}")
    # 返回成功——陈旧记录是待办事项而非错误
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """执行 verify 子命令：对草稿做机器取证并输出证据卡。

    退出码语义与 fetch 一致：取证过程本身的失败（草稿目录缺失、
    指定的 id 不存在）返回非零；而证据卡里的红/黄项**不**影响退出码——
    它们是给人判断的输入，不是程序失败。把「有待判断项」做成非零退出码，
    会让 CI 式的自动化误用这条命令当闸门，而它根本不是闸门。
    """
    # 加载全部草稿
    drafts, draft_errors = load_drafts()
    # 草稿目录级错误（目录缺失、文件损坏）必须明确报出并失败
    if draft_errors:
        # 逐条输出
        for err in draft_errors:
            # 打印到 stderr，避免污染 JSON 输出
            print(f"草稿加载问题：{err}", file=sys.stderr)
        # 目录不存在或文件无法解析属于取证失败
        if not drafts:
            # 返回失败码
            return EXIT_ISSUES

    # 确定本次要出证的草稿集合
    if args.all:
        # 全部草稿
        selected = drafts
    elif args.policy_id:
        # 指定单条；不存在时明确报错（静默对空集出证等于假装核过了）
        if args.policy_id not in drafts:
            # 打印错误
            print(f"未找到草稿：{args.policy_id}", file=sys.stderr)
            # 提示可用 id
            print(f"当前共有 {len(drafts)} 条草稿，可用 --all 全量出证", file=sys.stderr)
            # 返回失败码
            return EXIT_ISSUES
        # 只取指定的一条
        selected = {args.policy_id: drafts[args.policy_id]}
    else:
        # 既没给 id 也没给 --all：用法错误
        print("请指定草稿 id 或使用 --all", file=sys.stderr)
        # 返回用法错误码（与 argparse 一致）
        return 2

    # 逐条出证
    cards = build_all_cards(selected)

    # 需要落盘时逐张写 JSON 文件
    if args.output_dir is not None:
        # 确保目录存在
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # 逐张写入
        for pid, card in cards.items():
            # 计算路径
            path = args.output_dir / f"{pid}.json"
            # 写文件
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                # 序列化（保留中文）
                json.dump(card.to_dict(), fh, ensure_ascii=False, indent=2)

    # JSON 输出模式
    if args.json:
        # 单条时输出对象，多条时输出映射，便于两种用法都直接消费
        if args.policy_id and not args.all:
            # 单条：直接输出该卡
            print(json.dumps(cards[args.policy_id].to_dict(), ensure_ascii=False, indent=2))
        else:
            # 多条：输出 id 到卡的映射
            print(json.dumps({pid: card.to_dict() for pid, card in cards.items()}, ensure_ascii=False, indent=2))
    else:
        # 人类可读输出：逐张渲染
        for index, (_pid, card) in enumerate(cards.items()):
            # 多张之间空行分隔
            if index:
                # 分隔
                print()
            # 渲染并逐行输出
            for line in render_card_lines(card):
                # 输出
                print(line)
        # 落盘提示（落了才说，没说就是没落——可见性原则）
        if args.output_dir is not None:
            # 打印落盘位置
            print()
            print(f"证据卡已写入：{args.output_dir}（{len(cards)} 张）")

    # 成功返回（红/黄项不影响退出码，理由见函数文档）
    return EXIT_OK


def cmd_build_site(args: argparse.Namespace) -> int:
    """执行 build-site 子命令：把 data/ 渲染成静态站点。"""
    # 执行构建。时间戳与数据来源都不注入，由生成器自己取——
    # 命令行是「真实运行」的入口，只有测试才需要钉死这些值。
    result = build_site(output_dir=args.output)

    # JSON 输出模式
    if args.json:
        # 输出结构化结果，键名与 SiteBuildResult 字段一致
        print(
            json.dumps(
                {
                    "output_dir": result.output_dir,        # 输出目录
                    "page_count": result.page_count,        # 页面数
                    "policy_count": result.policy_count,    # 政策数
                    "change_count": result.change_count,    # 变更条目数
                    "generated_at": result.generated_at,    # 生成时间
                    "problems": result.problems,            # 构建本身的问题
                    "record_issue_count": result.record_issue_count,  # 记录校验提示数
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        # 人类可读输出
        print(f"站点已生成 —— {result.output_dir}")
        # 规模统计
        print(
            f"  {result.policy_count} 条政策、{result.page_count} 个页面、"
            f"{result.change_count} 条待处理变更"
        )
        # 预览提示。
        # 必须给出可执行的下一步：站点是纯静态的，用 file:// 直接打开会因
        # 浏览器的本地文件策略而让相对路径与脚本行为不一致，
        # 因此要引导使用者起一个本地 HTTP 服务。
        print()
        print("本地预览：")
        print(f"  cd {result.output_dir} && python -m http.server 8000")
        print("  然后浏览 http://localhost:8000/")

    # 构建过程中发现的问题必须显式报出，绝不静默通过。
    # 它们不阻断构建（站点要能展示「已知的部分」），但也不该被忽略——
    # 「少了几条记录」与「数据就是这样」在使用者眼里没有任何区别。
    if result.problems:
        # 留空行与上文分隔
        print()
        # 打印问题清单
        print(f"构建提示（{len(result.problems)} 项，未阻断构建）：")
        # 逐条输出
        for problem in result.problems:
            # 打印
            print(f"  - {problem}")

    # 数据记录层面的校验提示只给条数，把「看详情」导回 finreg validate。
    # 若在这里逐条打出十几行告警，上面那几行真正的构建问题就会被淹没，
    # 而告警一多，人就会习惯性忽略——那比不告警更糟。
    if result.record_issue_count:
        # 打印一行并指向真正的详情入口
        print(f"记录校验提示 {result.record_issue_count} 条（运行 finreg validate 查看详情）")

    # 构建本身不因数据问题失败——这与 fetch 的语义不同：
    # fetch 失败意味着「今天的数据没抓到」，是数据缺失；
    # build-site 失败意味着「页面没生成出来」，是发布事故。
    # 前者记问题、后者才该返回非零。因此这里恒定返回成功。
    return EXIT_OK


# ============================================================
# 入口
# ============================================================

# 子命令名到实现函数的映射
_COMMANDS = {
    "fetch": cmd_fetch,                       # 抓取
    "validate": cmd_validate,                 # 校验
    "validate-sources": cmd_validate_sources, # 只校验登记表
    "list": cmd_list,                         # 列出
    "show": cmd_show,                         # 查看单条
    "stale": cmd_stale,                       # 陈旧记录
    "verify": cmd_verify,                     # 草稿核验取证
    "build-site": cmd_build_site,             # 生成静态站点
}


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主入口。

    参数 argv 允许测试注入参数列表（如 ``main(["list", "--json"])``），
    避免测试依赖真实进程命令行。
    """
    # 构建解析器
    parser = build_parser()
    # 解析参数
    args = parser.parse_args(argv)
    # 取出对应的处理函数
    handler = _COMMANDS.get(args.command)
    # 理论上不会发生（argparse 已限制取值），但保持防御
    if handler is None:
        # 输出用法
        parser.print_help()
        # 返回用法错误码
        return 2
    # 执行并返回其退出码
    return handler(args)


# 作为脚本直接运行时执行主入口
if __name__ == "__main__":
    # 用 sys.exit 把退出码传给操作系统，使 CI 能据此判断成败
    sys.exit(main())
