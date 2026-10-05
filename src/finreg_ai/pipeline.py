"""流水线编排 —— 把抓取、比对、变更产出串成一条可重复执行的流程。

执行顺序
--------
::

    加载数据源登记表
        ↓
    逐源抓取（单源隔离，失败只影响自己）
        ↓
    加载库中已有政策记录
        ↓
    比对：抓到的条目 vs 已有记录 → 产出变更流
        ↓
    写入 data/changes/YYYY-MM-DD.json
        ↓
    输出报告（含失败源清单，避免「无变更」被误读为「一切正常」）

设计原则的落地
--------------
「单源失效不拖垮整体」不是一句口号，它体现在三处具体实现：

1. 每个源的抓取被独立 try/except 包裹，异常不会中断循环
2. 失败的源被记入 ``failed_sources``，最终体现在报告与变更流中
3. 部分源失败时流水线仍以退出码 0 结束（因为这是预期内的常态），
   但报告会明确列出失败源，并由 CI 决定是否告警
"""

# 导入 json 用于写变更流文件
import json
# 导入 dataclass 用于定义报告结构
from dataclasses import dataclass, field
# 导入 date 类型
from datetime import date
# 导入 Path 用于路径操作
from pathlib import Path
# 导入 Sequence 类型标注（来源为 collections.abc 而非 typing，理由见 cli.py）
from collections.abc import Sequence
# 导入 Any 类型标注
from typing import Any

# 导入本包各模块
# 注意：这里只导入实际用到的名字。看似「多导入几个备用」很方便，
# 但会让读者无法判断某个符号是否真的被使用，也掩盖了死代码。
from finreg_ai.diff import ChangeSet, diff_discovered
from finreg_ai.fetchers import build_fetcher, now_china_iso, today_china
from finreg_ai.fetchers.base import FetchResult
from finreg_ai.models import Source, is_warning, strip_warning_prefix
from finreg_ai.store import (
    CHANGES_DIR,                 # 变更流输出目录
    find_stale_policies,         # 陈旧记录查找
    load_all_policies,           # 加载全部政策
    load_sources,                # 加载数据源登记表
    validate_cross_references,   # 跨记录一致性校验
)


@dataclass
class PipelineReport:
    """一次流水线运行的完整报告。

    报告必须显式包含失败与降级的源，而不是只报告成功部分。
    这是刻意的反直觉设计：如果一个源静默失效，报告看起来「一切正常」，
    使用者就会误以为「最近没有新政策」——而真相是我们的抓取坏了。
    """

    # 运行日期
    run_on: date
    # 生成时间戳
    generated_at: str = field(default_factory=now_china_iso)
    # 各源的抓取结果
    fetch_results: list[FetchResult] = field(default_factory=list)
    # 最终产出的变更集合
    change_set: ChangeSet | None = None
    # 数据校验问题（结构、语义、跨引用）
    validation_errors: list[str] = field(default_factory=list)
    # 执行过程中的致命错误（如登记表损坏），非空时应中断整条流水线
    fatal_errors: list[str] = field(default_factory=list)

    @property
    def ok_sources(self) -> list[str]:
        """返回抓取正常的源列表。"""
        # 筛出 ok 与 empty 状态的源
        return [r.source_id for r in self.fetch_results if r.is_usable]

    @property
    def failed_sources(self) -> list[str]:
        """返回抓取失败的源列表。"""
        # 筛出 failed 与 degraded 状态的源
        return [r.source_id for r in self.fetch_results if not r.is_usable and r.status != "skipped"]

    @property
    def skipped_sources(self) -> list[str]:
        """返回本次跳过的源列表（禁用或人工录入源）。"""
        # 筛出 skipped 状态
        return [r.source_id for r in self.fetch_results if r.status == "skipped"]

    @property
    def total_docs(self) -> int:
        """返回本次抓到的条目总数。"""
        # 累加各源的条目数
        return sum(len(r.docs) for r in self.fetch_results)

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        # 构造输出结构
        return {
            "run_on": self.run_on.isoformat(),                  # 运行日期
            "generated_at": self.generated_at,                  # 生成时间
            "summary": {                                        # 概要统计
                "ok_sources": self.ok_sources,                  # 正常源
                "failed_sources": self.failed_sources,          # 失败源
                "skipped_sources": self.skipped_sources,        # 跳过源
                "total_docs_discovered": self.total_docs,       # 发现条目总数
            },
            "fetch_results": [                                  # 各源明细
                {
                    "source_id": r.source_id,                   # 源标识
                    "status": r.status,                         # 状态
                    "doc_count": len(r.docs),                   # 条目数
                    "dropped_navigation": r.dropped_navigation,  # 剔除的疑似导航链接数
                    # 关键词过滤丢弃数及其原因明细。必须暴露：一个源可能每天都在
                    # 丢掉若干条，而报告只显示 ok——把数字摆出来才能发现问题。
                    "dropped_by_filter": r.dropped_by_filter,        # 关键词过滤丢弃数
                    "filter_drop_reasons": r.filter_drop_reasons,    # 丢弃原因明细
                    # 被丢弃的条目本身（标题/链接/日期）。只有计数是无法行动的：
                    # 报告说「丢了 151 条」，维护者仍不知道该不该改关键词，
                    # 除非能看到丢掉的到底是哪 151 条。
                    #
                    # 为什么不放进公开的变更文件（data/changes/）而只放在报告里：
                    # 变更文件是面向使用者的数据产物，记录「有什么变化」；
                    # 这里是抓取期的诊断信息，记录「流水线做过什么取舍」。
                    # 把后者塞进前者，会让公开产物里混进大量与政策无关的标题。
                    "dropped_filter_docs": [
                        {
                            "title": d.title,                                    # 标题
                            "url": d.url,                                        # 详情页链接
                            "published_on": d.published_on.isoformat() if d.published_on else None,  # 发布日期
                        }
                        for d in r.dropped_filter_docs                          # 逐条序列化
                    ],
                    "error": r.error,                           # 错误说明
                    "request_count": r.request_count,           # 请求次数
                    "fetched_at": r.fetched_at,                 # 抓取时间
                }
                for r in self.fetch_results
            ],
            "change_set": self.change_set.to_dict() if self.change_set else None,  # 变更集合
            "validation_errors": self.validation_errors,        # 校验问题
            "fatal_errors": self.fatal_errors,                  # 致命错误
        }


# ============================================================
# 抓取阶段
# ============================================================

def fetch_all(
    sources: dict[str, Source],
    source_ids: Sequence[str] | None = None,
) -> list[FetchResult]:
    """逐源抓取，返回各源结果列表。

    参数 source_ids 为 None 时抓取全部启用的源；
    指定时只抓取列出的源（用于手动重试单个失败的源）。
    """
    # 结果容器
    results: list[FetchResult] = []
    # 遍历数据源
    for sid, source in sources.items():
        # 指定了源列表时跳过不在其中的
        if source_ids is not None and sid not in source_ids:
            # 跳过该源
            continue
        # 构造抓取器
        fetcher = build_fetcher(source)
        try:
            # 执行抓取
            result = fetcher.fetch()
        except Exception as exc:  # noqa: BLE001  这是「单源隔离」的最后一道保险
            # 构造失败结果而非让异常中断循环。
            # 注意基类的 fetch() 内部已捕获绝大多数异常，
            # 这里捕获的是构造抓取器本身出错等意外情况。
            result = FetchResult(
                source_id=sid,                                          # 源标识
                status="failed",                                        # 失败状态
                error=f"抓取器异常：{type(exc).__name__}: {exc}",         # 错误说明
            )
        # 收集结果
        results.append(result)
    # 返回全部结果
    return results


# ============================================================
# 变更产出阶段
# ============================================================

def write_change_set(change_set: ChangeSet, directory: Path | None = None) -> Path:
    """把变更集合写入 ``data/changes/YYYY-MM-DD.json``，返回写入路径。"""
    # 未指定目录时使用默认变更目录
    target = directory or CHANGES_DIR
    # 确保目录存在
    target.mkdir(parents=True, exist_ok=True)
    # 计算目标路径，以日期命名便于按时间检索
    path = target / f"{change_set.detected_on.isoformat()}.json"
    # 写入 JSON；ensure_ascii=False 保证中文可读，indent=2 便于人工查看 diff
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        # 序列化并写入
        json.dump(change_set.to_dict(), fh, ensure_ascii=False, indent=2)
        # 补一个换行，符合 POSIX 规范
        fh.write("\n")
    # 返回写入路径
    return path


# ============================================================
# 主流程
# ============================================================

def run_pipeline(
    source_ids: Sequence[str] | None = None,
    today: date | None = None,
    write_changes: bool = True,
) -> PipelineReport:
    """执行完整流水线，返回报告。

    参数 today 允许注入固定日期以便测试；
    参数 write_changes 控制是否落盘变更流文件（测试时可关）。
    """
    # 确定运行日期：未指定时取北京时间今天
    run_on = today or today_china()
    # 初始化报告
    report = PipelineReport(run_on=run_on)

    # --- 第 1 步：加载数据源登记表 ---
    issuers, sources, source_errors = load_sources()
    # 登记表有结构性问题属致命错误，直接返回
    if source_errors:
        # 记入致命错误
        report.fatal_errors.extend(source_errors)
        # 终止流程——没有可用的源定义，后续步骤没有意义
        return report

    # --- 第 2 步：加载库中已有政策记录 ---
    policies, policy_errors = load_all_policies()
    # 数据校验问题记入报告但不中断——已有数据的问题不应阻止抓取新内容
    report.validation_errors.extend(policy_errors)

    # --- 第 3 步：跨记录一致性校验 ---
    # 版本链闭合性检查：声称被某文件取代，该文件必须存在
    report.validation_errors.extend(validate_cross_references(policies))

    # --- 第 4 步：逐源抓取（单源隔离） ---
    report.fetch_results = fetch_all(sources, source_ids)

    # --- 第 5 步：比对，产出变更 ---
    # 收集本次抓到的全部条目
    all_docs = [doc for result in report.fetch_results for doc in result.docs]
    # 已有记录的官方链接集合，用于判断哪些条目尚未录入
    known_urls = {p.source.url for p in policies.values()}
    # 检测已有记录之间的变更（需要一个「旧版本」作对比基准；
    # 此处以磁盘上的当前状态作为新版本，旧版本由 CI 从 git 历史取得，
    # 在没有 git 上下文时跳过这一步）
    # 这里先只处理「发现新条目」这一类变更
    discovered = diff_discovered(all_docs, known_urls, run_on)

    # 构造变更集合
    change_set = ChangeSet(
        detected_on=run_on,                                                       # 检测日期
        changes=discovered,                                                       # 变更列表
        policies_scanned=len(policies),                                           # 扫描政策数
        failed_sources=report.failed_sources,                                     # 失败源列表
    )
    # 记入报告
    report.change_set = change_set

    # --- 第 6 步：落盘变更流 ---
    if write_changes:
        # 写入文件
        write_change_set(change_set)

    # 返回报告
    return report


def split_issues(issues: Sequence[str]) -> tuple[list[str], list[str]]:
    """把校验问题拆成 ``(错误, 警告)`` 两份清单。

    拆分的意义：错误必须阻断，警告只需提示。
    如果不拆，维护者会遇到「明明是已知的待办事项，却让 CI 红掉」的困扰，
    最终结果是他们会去删掉那条警告——问题从「可见的待办」变成
    「不可见的技术债」，这比一开始就不检查更糟。
    """
    # 错误清单
    errors: list[str] = []
    # 警告清单
    warnings: list[str] = []
    # 逐条分流
    for issue in issues:
        # 警告级问题进入警告清单
        if is_warning(issue):
            # 去掉前缀后收集，便于展示
            warnings.append(strip_warning_prefix(issue))
        else:
            # 其余为错误
            errors.append(issue)
    # 返回两份清单
    return errors, warnings


def validate_repository(
    today: date | None = None, stale_threshold_days: int = 180
) -> tuple[list[str], list[str], list[tuple[str, int]]]:
    """校验整个仓库的数据完整性。

    返回 ``(错误列表, 警告列表, 陈旧记录列表)`` 三元组。

    这是 CI 中 ``finreg validate`` 命令的实现，也是保证数据质量不随
    贡献者增多而退化的机制。任何人提交的数据都要过这一关。
    """
    # 确定基准日期
    ref_day = today or today_china()
    # 收集全部原始问题（含警告前缀）
    raw_issues: list[str] = []

    # 校验数据源登记表
    _issuers, _sources, source_errors = load_sources()
    # 收集登记表问题
    raw_issues.extend(source_errors)

    # 校验全部政策记录
    policies, policy_errors = load_all_policies()
    # 收集记录问题
    raw_issues.extend(policy_errors)

    # 跨记录一致性校验（这类问题一律为错误，不含警告）
    raw_issues.extend(validate_cross_references(policies))

    # 拆分错误与警告
    errors, warnings = split_issues(raw_issues)

    # 查找陈旧记录
    stale = find_stale_policies(policies, ref_day, stale_threshold_days)

    # 返回结果
    return errors, warnings, stale
