"""命令行入口 —— 通过 ``finreg`` 命令使用本项目。

提供五组命令：

===============  ====================================================
命令              用途
===============  ====================================================
``fetch``         执行抓取流水线，发现新政策并产出变更流
``validate``      校验仓库全部数据的完整性（CI 使用）
``list``          列出政策记录，支持按状态、机构、相关度筛选
``show``          查看单条政策记录的完整信息
``stale``         列出超期未核验的记录（时效性治理的核心工具）
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
# 导入 Sequence 类型标注。
# 注意这里从 collections.abc 而非 typing 导入：Python 3.9 起
# typing.Sequence 等别名已被标记为弃用，泛型容器类型应直接使用标准库抽象基类。
from collections.abc import Sequence

# 导入包版本号
from finreg_ai import __version__
# 从抓取器包导入日期工具
from finreg_ai.fetchers.base import today_china
# 导入政策对象的字典化函数，用于 show --json 输出。
# 早期这里不在模块顶部导入，而是在一个包装函数内部做局部 import，
# 那个包装函数只是原样转调 policy_to_dict，没有任何附加逻辑——
# 属于「为了一个已经不成立的顾虑而多写一层」。现在直接导入使用。
from finreg_ai.models import policy_to_dict
# 导入流水线函数
from finreg_ai.pipeline import run_pipeline, validate_repository
# 导入数据加载函数
from finreg_ai.store import load_all_policies, load_sources

# 退出码约定：0 成功，1 发现问题（校验失败），2 用法错误（argparse 默认）
EXIT_OK = 0       # 一切正常
EXIT_ISSUES = 1   # 发现数据问题


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

    # 返回解析器
    return parser


# ============================================================
# 各子命令实现
# ============================================================

def cmd_fetch(args: argparse.Namespace) -> int:
    """执行 fetch 子命令。"""
    # 执行流水线
    report = run_pipeline(
        source_ids=args.sources,            # 指定的源列表（可能为 None）
        write_changes=not args.dry_run,     # dry-run 时不写文件
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
            # 有被剔除的导航链接时单独标注。
            # 这一项必须显示：清洗只要不可见就会变成新的静默风险——
            # 某天一次改版让大量真实条目被误判为导航，而报告依旧显示 ok。
            if result.dropped_navigation:
                # 打印剔除数量
                print(f"           ├─ 已剔除 {result.dropped_navigation} 条疑似栏目导航链接")
            # 有错误信息时缩进打印
            if result.error:
                # 打印错误说明
                print(f"           └─ {result.error}")

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
    # 以北京时间为基准
    ref_day = today_china()
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

    # 打印标题
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
