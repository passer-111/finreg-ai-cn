"""记录级变更检测的测试 —— ``diff.detect_policy_changes`` 与基准接线。

为什么这个文件存在
------------------
``detect_policy_changes`` 曾是死代码：流水线只调用 ``diff_discovered``，
它自写完之日起从未在真实运行中执行过，也因此一个测试都没有。
本项目的教训之一是「空转的规则比没有规则更危险」——没有测试的检测逻辑
即使接进流水线，也无法保证它检测的是它声称检测的东西。

这组测试覆盖四类变更（新增 / 状态 / 内容与元数据 / 陈旧）与基准接线，
全部离线：基准记录通过临时目录里的 YAML 注入，不碰仓库真实数据。
"""

# 导入 date：构造固定基准日与日期差
from datetime import date
# 导入 Path：构造临时基准目录
from pathlib import Path

# 导入 yaml：把基准记录写成 YAML 文件（与仓库数据同构）
import yaml

# 导入变更类型常量：断言用常量而非裸字符串，避免与生产代码各自演化
from finreg_ai.diff import (
    CHANGE_TYPE_CONTENT,      # 正文变更
    CHANGE_TYPE_METADATA,     # 元数据变更
    CHANGE_TYPE_NEW,          # 首次收录
    CHANGE_TYPE_STATUS,       # 状态变更
    detect_policy_changes,    # 待测的检测函数
)
# 导入模型：构造溯源信息与日期类型
from finreg_ai.models import PolicyStatus, SourceRef, policy_to_dict
# 导入基准接线函数
from finreg_ai.pipeline import detect_baseline_changes
# 复用政策工厂，避免把 30 行构造逻辑维护两遍
from tests.test_models import make_policy

# 固定的检测日期。写死而非用「今天」，测试才不与运行日期耦合——
# 会随时间自行变红的测试比没有测试更糟，本项目已因此吃过亏。
FIXED_DAY = date(2026, 10, 7)


def make_source(content_hash: str | None = None) -> SourceRef:
    """构造一个溯源对象，可指定正文哈希。"""
    # 逐字段构造，与 make_policy 默认值同形
    return SourceRef(
        url="https://example.gov.cn/a.html",        # 官方链接
        site="example.gov.cn",                      # 站点域名
        fetched_at="2026-10-03T12:00:00+08:00",     # 抓取时间
        content_hash=content_hash,                  # 正文哈希（可空）
    )


# ============================================================
# 第一类：新增记录
# ============================================================

def test_detect_new_record_is_reported_with_significance() -> None:
    """验证「new 中有而 old 中没有」的记录被报为首次收录。"""
    # 旧集合为空
    old = {}
    # 新集合含一条现行有效且 AI 核心相关的政策
    policy = make_policy(id="test-2026-new")
    # 检测
    changes = detect_policy_changes(old, {"test-2026-new": policy}, FIXED_DAY)
    # 应只有一条变更
    assert len(changes) == 1
    # 类型为首次收录
    assert changes[0].change_type == CHANGE_TYPE_NEW
    # 现行有效 + 核心相关 → 高重要性
    assert changes[0].significance == "high"
    # 附带官方链接，便于回源核对
    assert changes[0].url == policy.source.url


def test_detect_new_record_with_lower_significance_when_not_core() -> None:
    """反向测试：非现行有效或非核心的新记录不应是高重要性。

    若重要性被写死成 high，上一条测试仍会通过，只有这条会红——
    两条一起构成夹逼。
    """
    # 新集合含一条征求意见稿（非现行有效）
    policy = make_policy(id="test-2026-draft", status=PolicyStatus.CONSULTATION, effective_from=None)
    # 检测
    changes = detect_policy_changes({}, {"test-2026-draft": policy}, FIXED_DAY)
    # 重要性应为中
    assert changes[0].significance == "medium"


# ============================================================
# 第二类：状态变更
# ============================================================

def test_detect_status_change_is_high_significance() -> None:
    """验证法律状态变化被报为最高优先级的 status_changed。"""
    # 旧版本：征求意见稿
    old_policy = make_policy(id="test-2026-status", status=PolicyStatus.CONSULTATION, effective_from=None)
    # 新版本：现行有效
    new_policy = make_policy(id="test-2026-status", status=PolicyStatus.EFFECTIVE)
    # 检测
    changes = detect_policy_changes({"test-2026-status": old_policy}, {"test-2026-status": new_policy}, FIXED_DAY)
    # 按类型取出状态变更条目
    status_changes = [c for c in changes if c.change_type == CHANGE_TYPE_STATUS]
    # 必须恰好一条
    assert len(status_changes) == 1
    # 状态变更一律高优先级
    assert status_changes[0].significance == "high"
    # 涉及字段为 status
    assert status_changes[0].fields == ["status"]
    # 说明里要写出从什么状态变到什么状态
    assert "consultation" in status_changes[0].detail
    assert "effective" in status_changes[0].detail


# ============================================================
# 第三类：内容与元数据变更
# ============================================================

def test_detect_content_hash_change_is_content_changed() -> None:
    """验证正文哈希变化被报为 content_changed（需重做合规评估）。

    正文哈希变化意味着「官方可能在未发通知的情况下修改了文件」，
    这是合规场景里最需要重新评估的一类变更，因此单独成类且高优先级。
    """
    # 旧版本带哈希 aaa
    old_policy = make_policy(id="test-2026-hash", source=make_source("sha256:" + "a" * 64))
    # 新版本带哈希 bbb
    new_policy = make_policy(id="test-2026-hash", source=make_source("sha256:" + "b" * 64))
    # 检测
    changes = detect_policy_changes({"test-2026-hash": old_policy}, {"test-2026-hash": new_policy}, FIXED_DAY)
    # 应只有一条正文变更
    assert len(changes) == 1
    # 类型为正文变更
    assert changes[0].change_type == CHANGE_TYPE_CONTENT
    # 高重要性
    assert changes[0].significance == "high"


def test_detect_metadata_change_is_low_significance() -> None:
    """验证仅元数据变化（如生效日期）被报为低优先级的 metadata_changed。

    元数据变更不需要重新做合规评估，若与正文变更同级，
    变更流会被噪声淹没，最终无人阅读。
    """
    # 旧版本生效日期 2026-07-01
    old_policy = make_policy(id="test-2026-meta", effective_from=date(2026, 7, 1))
    # 新版本生效日期延后到 2026-08-01（政策延期生效是常见情况）
    new_policy = make_policy(id="test-2026-meta", effective_from=date(2026, 8, 1))
    # 检测
    changes = detect_policy_changes({"test-2026-meta": old_policy}, {"test-2026-meta": new_policy}, FIXED_DAY)
    # 应只有一条元数据变更
    assert len(changes) == 1
    # 类型为元数据变更
    assert changes[0].change_type == CHANGE_TYPE_METADATA
    # 低重要性
    assert changes[0].significance == "low"
    # 涉及字段为生效日期
    assert changes[0].fields == ["effective_from"]


def test_detect_no_change_for_identical_records() -> None:
    """反向测试：完全一致的记录不产生任何变更。

    若检测逻辑把「每条记录」都报一遍，上面的测试仍可能通过
    （它们只断言「有」），只有这条断言「没有」能拦住误报。
    """
    # 新旧集合完全相同
    policy = make_policy(id="test-2026-same")
    # 检测
    changes = detect_policy_changes({"test-2026-same": policy}, {"test-2026-same": policy}, FIXED_DAY)
    # 不应有任何变更（记录核验日距基准日仅 4 天，也不触发陈旧告警）
    assert changes == []


# ============================================================
# 第四类：陈旧告警
# ============================================================

def test_detect_stale_record_raises_alert() -> None:
    """验证超过 180 天未核验的记录浮出陈旧告警。

    这条告警不是「政策变更」而是「记录维护滞后」——
    不进入变更流，陈旧会永远无人处理。
    """
    # 核验日距今 200 天
    stale_policy = make_policy(id="test-2026-stale", last_verified=date(2026, 3, 21))
    # 新旧集合相同（陈旧告警只看当前状态）
    policies = {"test-2026-stale": stale_policy}
    # 检测
    changes = detect_policy_changes(policies, policies, FIXED_DAY)
    # 应只有一条陈旧告警
    assert len(changes) == 1
    # 说明里点明是陈旧问题
    assert "记录陈旧" in changes[0].detail
    # 中优先级
    assert changes[0].significance == "medium"


def test_detect_stale_boundary_at_exactly_threshold() -> None:
    """反向夹逼：恰好 180 天不告警，181 天才告警。

    判定符是 ``days > threshold``，「恰好等于阈值」不会触发。
    若实现被改成 ``>=``，这条测试会红；若阈值被悄悄改小，同样会红。
    """
    # 核验日恰好距今 180 天
    at_boundary = make_policy(id="test-2026-edge", last_verified=date(2026, 4, 10))
    # 检测
    policies = {"test-2026-edge": at_boundary}
    # 不应有陈旧告警
    assert detect_policy_changes(policies, policies, FIXED_DAY) == []


# ============================================================
# 排序
# ============================================================

def test_detect_changes_sorted_by_significance() -> None:
    """验证高优先级变更排在低优先级之前，便于变更流阅读。"""
    # 构造三类变更：一条元数据（low）、一条状态（high）、一条新增有效核心（high）
    old = {
        "test-2026-meta": make_policy(id="test-2026-meta", effective_from=date(2026, 7, 1)),
        "test-2026-status": make_policy(id="test-2026-status", status=PolicyStatus.CONSULTATION, effective_from=None),
    }
    # 新集合
    new = {
        "test-2026-meta": make_policy(id="test-2026-meta", effective_from=date(2026, 8, 1)),
        "test-2026-status": make_policy(id="test-2026-status", status=PolicyStatus.EFFECTIVE),
        "test-2026-new": make_policy(id="test-2026-new"),
    }
    # 检测
    changes = detect_policy_changes(old, new, FIXED_DAY)
    # 重要性序列必须单调不增（high 在前）
    order = {"high": 0, "medium": 1, "low": 2}
    # 取出序号
    ranks = [order[c.significance] for c in changes]
    # 断言有序
    assert ranks == sorted(ranks)


# ============================================================
# 基准接线（detect_baseline_changes）
# ============================================================

def write_baseline(directory: Path, policies: list) -> None:
    """把政策对象列表写成基准目录里的 YAML 文件（与仓库数据同构）。"""
    # 逐个写出
    for policy in policies:
        # 文件名与 id 一一对应（save_policy 的同名约定）
        (directory / f"{policy.id}.yaml").write_text(
            # policy_to_dict 已把日期与枚举转成字符串，可直接序列化
            yaml.safe_dump(policy_to_dict(policy), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def test_baseline_changes_detects_difference_against_old_records(tmp_path: Path) -> None:
    """验证基准目录中的旧版记录与当前记录对比后产出变更事件。"""
    # 旧版：征求意见稿。
    # domain 与 issuer_code 必须用 schema 受控词表里的取值：
    # 基准加载走与仓库数据完全相同的 schema 校验（这是刻意的，见
    # detect_baseline_changes 的说明），工厂默认值 "banking" 不在词表内。
    old_policy = make_policy(
        id="test-2026-base",
        status=PolicyStatus.CONSULTATION,
        effective_from=None,
        domain=["银行", "人工智能"],
        issuer_code="nfra",
    )
    # 写入基准目录
    write_baseline(tmp_path, [old_policy])
    # 当前记录：已转为现行有效
    current = {
        "test-2026-base": make_policy(
            id="test-2026-base",
            status=PolicyStatus.EFFECTIVE,
            domain=["银行", "人工智能"],
            issuer_code="nfra",
        )
    }
    # 检测
    changes, errors = detect_baseline_changes(tmp_path, current, FIXED_DAY)
    # 基准加载不应有错误
    assert errors == []
    # 必须包含状态变更事件
    assert any(c.change_type == CHANGE_TYPE_STATUS for c in changes)


def test_baseline_changes_reports_missing_directory(tmp_path: Path) -> None:
    """验证基准目录不存在时给出带「基准记录」前缀的错误，而非静默跳过。

    CI 里基准目录由 git archive 导出，导出失败（如目录被改名）时
    若静默跳过，记录级检测会无声消失——变更流看起来正常，
    实际上一整类变更事件不再产生。
    """
    # 指向不存在的目录
    changes, errors = detect_baseline_changes(tmp_path / "不存在", {}, FIXED_DAY)
    # 无变更产出
    assert changes == []
    # 有且仅有带前缀的错误
    assert len(errors) == 1
    # 前缀便于区分错误来自基准还是当前记录
    assert errors[0].startswith("基准记录：")


def test_baseline_changes_prefixes_load_errors(tmp_path: Path) -> None:
    """验证基准文件损坏时，错误同样带「基准记录」前缀且检测继续。

    一条损坏的基准记录不该让整条检测失败（其余记录的比对仍然有效），
    但损坏必须可见——前缀让维护者一眼知道该去修哪一份数据。
    """
    # 写一个空文件（load_policy_file 视空文件为错误）
    (tmp_path / "broken.yaml").write_text("", encoding="utf-8")
    # 检测
    changes, errors = detect_baseline_changes(tmp_path, {}, FIXED_DAY)
    # 检测仍然完成（没有可比对的记录，产出为空）
    assert changes == []
    # 错误带前缀
    assert any(e.startswith("基准记录：") for e in errors)


def test_baseline_changes_drops_semantic_warnings(tmp_path: Path) -> None:
    """验证基准记录的**警告级**问题被丢弃，不与当前记录的报告重复。

    基准通常与当前记录是同一份数据（CI 从 git HEAD 导出），
    它的语义警告（如「现行有效但未填生效日期」）当前记录那边已报过一遍。
    再报一次警告数翻倍——告警一多，人就会习惯性忽略。
    反向用例：若不过滤，本条断言 ``errors == []`` 必红。
    """
    # 构造一条「现行有效但未填生效日期」的记录——必产生警告级问题，
    # 且 domain/issuer_code 取 schema 受控值，确保没有错误级问题混入
    warning_only = make_policy(
        id="test-2026-warn",
        status=PolicyStatus.EFFECTIVE,
        effective_from=None,
        domain=["银行", "人工智能"],
        issuer_code="nfra",
    )
    # 写入基准目录
    write_baseline(tmp_path, [warning_only])
    # 当前集合用同一条记录（检测本身也应产出空）
    changes, errors = detect_baseline_changes(tmp_path, {"test-2026-warn": warning_only}, FIXED_DAY)
    # 无变更
    assert changes == []
    # 警告级问题被丢弃，不上报
    assert errors == []
