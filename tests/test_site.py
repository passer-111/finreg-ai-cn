"""静态站点生成器的测试。

全部离线，且全部注入固定时间戳
------------------------------
站点页脚会写「页面生成于 …」。若测试不注入时间戳，两次构建的产物就必然不同，
于是「构建是确定性的」这条性质无法被验证——而确定性恰恰是这个设计的关键：
**站点是 git 数据的一个纯函数式视图**，同一份数据必须产出逐字节相同的页面。
一旦引入时间或随机，就再也说不清「页面变了」是因为数据变了还是因为跑的时间变了。

这是本项目第二次遇到同类问题。上一次是 `stale` 命令的基准日：
一条断言依赖「记录核验日期是今天」，两天后同一条断言自行变红而代码一行未改。
结论相同：**会随时间自行变红的测试比没有测试更糟。**
"""

# 导入 json：断言 JSON API 的结构
import json
# 导入 Path：构造临时目录与静态源目录
from pathlib import Path
# 导入 datetime/timezone/timedelta：构造固定时间戳
from datetime import datetime, timedelta, timezone

# 导入 pytest：临时目录装置与异常断言
import pytest

# 导入政策模型：构造测试记录
from finreg_ai.models import PolicyStatus
# 导入站点生成器
from finreg_ai.site import (
    build_site,                 # 构建入口
    load_change_documents,      # 变更文件加载
    render_changes_page,        # 变更流页渲染
    render_policy_page,         # 详情页渲染
)
# 导入 schema 校验工具：用来验证 JSON API 与 schema 保持一致
from finreg_ai.store import load_policy_schema, validate_against_schema
# 复用 test_models 里已有的政策工厂，避免把同一个 30 行的构造逻辑维护两遍。
# 跨测试模块 import 在本项目是可接受的：tests 已经是真正的包（含 __init__.py），
# `test_fetchers_offline.py` 同样从 `tests.conftest` 导入。
from tests.test_models import make_policy

# 固定时间戳：东八区 2026-10-06 12:00。
# 显式带时区，与生产代码 `datetime.now(CHINA_TZ)` 的形态一致。
FIXED_TIME = datetime(2026, 10, 6, 12, 0, tzinfo=timezone(timedelta(hours=8)))


# ============================================================
# 测试辅助
# ============================================================

def make_site_source(tmp_path: Path) -> Path:
    """构造一个最小的静态源目录（含两个占位资源文件）。

    刻意不指向仓库里真实的 ``site/``：测试若依赖真实资源文件，
    有人重命名 CSS 时测试会红在生产代码上，而真正该被测试的
    「资源复制逻辑」反而没被覆盖。
    """
    # 创建 assets 目录
    assets = tmp_path / "site" / "assets"
    # parents=True 允许一次性建出多级目录
    assets.mkdir(parents=True, exist_ok=True)
    # 写一个占位样式文件
    (assets / "style.css").write_text("/* 测试样式 */\n", encoding="utf-8")
    # 写一个占位脚本文件
    (assets / "app.js").write_text("// 测试脚本\n", encoding="utf-8")
    # 写一个占位图标文件（内容无关紧要，复制逻辑只关心「文件在不在」）
    (assets / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    # 返回站点源目录
    return tmp_path / "site"


def make_change_document(day: str = "2026-10-05", changes: list[dict] | None = None) -> dict:
    """构造一份变更文件内容。"""
    # 返回与 data/changes/YYYY-MM-DD.json 同构的字典
    return {
        "detected_on": day,                       # 检测日期
        "generated_at": f"{day}T20:53:23+08:00",  # 生成时间
        "policies_scanned": 9,                    # 已收录政策数
        "change_count": len(changes or []),       # 变更条数
        "high_significance_count": 0,             # 高重要度条数
        "failed_sources": [],                     # 失败的源
        "changes": changes if changes is not None else [],  # 明细
    }


def make_change(**overrides: object) -> dict:
    """构造变更明细里的一条记录。"""
    # 默认值取「一切正常」的形态
    defaults: dict = {
        "change_type": "discovered",                        # 变更类型
        "policy_id": "pending:test-source:https://example.gov.cn/a.html",  # 标识
        "title": "某条待录入的发现",                          # 标题
        "detected_on": "2026-10-05",                        # 检测日期
        "significance": "medium",                           # 重要度
        "detail": "发现新条目待录入：某条待录入的发现",          # 明细
        "url": "https://example.gov.cn/a.html",             # 官方链接
        "fields": [],                                       # 变更字段
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 返回
    return defaults


def build_into(
    tmp_path: Path,
    **overrides: object,
) -> tuple[object, Path]:
    """在临时目录里构建一次站点，返回（构建结果, 输出目录）。"""
    # 输出目录
    out = tmp_path / "docs"
    # 静态源目录
    site_source = make_site_source(tmp_path)
    # 组装参数：默认值可被覆盖项顶替
    kwargs: dict = {
        "output_dir": out,                  # 输出目录
        "site_source_dir": site_source,     # 静态源目录
        "policies": {},                     # 默认无政策
        "change_documents": [],             # 默认无变更
        "generated_at": FIXED_TIME,         # 固定时间戳
    }
    # 应用覆盖项
    kwargs.update(overrides)
    # 构建
    result = build_site(**kwargs)  # type: ignore[arg-type]
    # 返回
    return result, out


# ============================================================
# 产物完整性
# ============================================================

def test_build_site_writes_every_expected_file(tmp_path: Path) -> None:
    """验证一次构建会产出全部约定文件。"""
    # 一条政策 + 一份变更
    policy = make_policy()
    # 构建
    result, out = build_into(
        tmp_path,
        policies={policy.id: policy},
        change_documents=[make_change_document(changes=[make_change()])],
    )
    # 约定产出的相对路径清单
    expected = {
        "index.html",                                   # 首页
        "changes.html",                                 # 变更流
        f"policies/{policy.id}.html",                   # 详情页
        "data/policies.json",                           # 政策 JSON API
        "data/changes.json",                            # 变更 JSON API
        "assets/style.css",                             # 样式
        "assets/app.js",                                # 检索脚本
        "assets/favicon.ico",                           # 站点图标
        ".nojekyll",                                    # 关闭 Jekyll 处理的标记
    }
    # 逐个断言文件真实存在（用 returned 的 written 清单对比磁盘会让断言失真）
    for relative in expected:
        # 文件必须存在于磁盘
        assert (out / relative).is_file(), f"缺少产物：{relative}"
    # written 清单必须与实际产出一致，否则 CLI 的输出会骗人
    assert set(result.written) == expected


def test_build_site_counts_pages_policies_and_changes(tmp_path: Path) -> None:
    """验证构建结果里的统计数字。"""
    # 两条政策
    first = make_policy(id="test-2026-alpha")
    # 第二条
    second = make_policy(id="test-2026-beta")
    # 构建
    result, _out = build_into(
        tmp_path,
        policies={first.id: first, second.id: second},
        change_documents=[
            make_change_document(changes=[make_change(), make_change()]),   # 2 条
            make_change_document(day="2026-10-04", changes=[make_change()]),  # 1 条
        ],
    )
    # 页面数 = 首页 + 变更流 + 两条详情页
    assert result.page_count == 4
    # 政策数
    assert result.policy_count == 2
    # 变更条目数跨文件累加
    assert result.change_count == 3


def test_build_site_writes_lf_line_endings(tmp_path: Path) -> None:
    """验证产物使用 LF 换行。

    生成物若混入 CRLF，同一次构建在 Windows 与 Linux 上会产生不同的字节，
    「构建是确定性的」这句话就不成立了，CI 上的 diff 也会整文件变红。
    """
    # 构建
    _result, out = build_into(tmp_path, policies={p.id: p for p in [make_policy()]})
    # 读取首页的原始字节
    raw = (out / "index.html").read_bytes()
    # 不允许出现 CRLF
    assert b"\r\n" not in raw


def test_build_site_writes_valid_json_apis(tmp_path: Path) -> None:
    """验证两个 JSON API 文件都是合法 JSON 且结构符合预期。"""
    # 政策
    policy = make_policy()
    # 构建
    _result, out = build_into(
        tmp_path,
        policies={policy.id: policy},
        change_documents=[make_change_document(changes=[make_change()])],
    )
    # 解析政策 API
    policies_api = json.loads((out / "data" / "policies.json").read_text(encoding="utf-8"))
    # 顶层结构
    assert set(policies_api) == {"generated_at", "count", "policies"}
    # 条数一致
    assert policies_api["count"] == 1
    # 解析变更 API
    changes_api = json.loads((out / "data" / "changes.json").read_text(encoding="utf-8"))
    # 变更文件原样透传，因此天数为 1
    assert len(changes_api["days"]) == 1


# ============================================================
# 确定性与时间解耦
# ============================================================

def test_build_site_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """验证同一份数据 + 同一时间戳 → 逐字节相同的产物。

    这是整个设计的地基：站点必须是 git 数据的**纯函数**。
    若这条不成立，「页面变了」就无法归因到「数据变了」，
    而本项目的全部价值恰恰建立在「变更可归因」之上。
    """
    # 准备数据
    policy = make_policy()
    # 变更
    changes = [make_change_document(changes=[make_change()])]

    # 第一次构建到 docs-a
    first_result, first_out = build_into(
        tmp_path,
        policies={policy.id: policy},
        change_documents=changes,
    )
    # 第二次构建到 docs-b：把输出目录换掉，其余输入完全一致
    second_result, second_out = build_into(
        tmp_path,
        policies={policy.id: policy},
        change_documents=changes,
        output_dir=tmp_path / "docs-b",
    )

    # 两次的产物清单必须一致
    assert sorted(first_result.written) == sorted(second_result.written)
    # 逐文件比对字节
    for relative in first_result.written:
        # 读取第一次的字节
        a = (first_out / relative).read_bytes()
        # 读取第二次的字节
        b = (second_out / relative).read_bytes()
        # 必须逐字节相同
        assert a == b, f"产物不一致：{relative}"


def test_build_site_generated_at_comes_from_injected_value(tmp_path: Path) -> None:
    """验证注入的时间戳真的被写进了产物，而不是被忽略。

    若只断言「两次构建相同」，生产代码改成忽略参数、永远用 now() 也能通过
    （两次构建发生在同一秒内时）。必须同时钉住时间戳确实来自入参。
    """
    # 构建
    result, out = build_into(tmp_path)
    # 结果里的时间戳应与注入值一致（格式化为 YYYY-MM-DD HH:MM +0800）
    assert result.generated_at == "2026-10-06 12:00 +0800"
    # 页脚必须出现该时间戳
    assert "+0800" in (out / "index.html").read_text(encoding="utf-8")


def test_build_site_default_timestamp_uses_china_timezone(tmp_path: Path) -> None:
    """验证未注入时间戳时页脚使用北京时间（+0800），而非构建机本地时区。

    反向用例：生产代码若退回 ``datetime.now().astimezone()``，在 UTC 机器上
    （GitHub 运行器正是 UTC）页脚会显示 +0000，本测试即红——
    而抓取层时间戳一律是 +08:00（fetchers.base.now_china_iso），
    两种口径并存会让读者无法判断两个时间是不是同一时刻。
    这条断言只依赖时区行为、不依赖「今天是哪天」，因此不会随时间漂移。
    """
    # 不注入 generated_at，让生产代码自己取当前时刻
    result, out = build_into(tmp_path, generated_at=None)
    # 结果里的时间戳必须以东八区偏移结尾
    assert result.generated_at.endswith("+0800")
    # 页脚同样必须是东八区
    assert "+0800" in (out / "index.html").read_text(encoding="utf-8")


# ============================================================
# HTML 转义
# ============================================================

def test_build_site_escapes_html_in_policy_title(tmp_path: Path) -> None:
    """验证政策标题里的 HTML 会被转义。

    站点是纯静态的、没有服务端，转义是**唯一**的注入防线。
    标题字段来自官方页面标题的抄录，理论上不该含标签，但
    「理论上不该」不是安全边界——一旦漏出去，一个政策标题就能改写整个页面。
    """
    # 构造带标签与引号的标题
    policy = make_policy(title='<script>alert("xss")</script> 测试政策')
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 读首页
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 原始的 <script 标签绝不能出现
    assert "<script>alert" not in index_html
    # 应被转义成实体
    assert "&lt;script&gt;" in index_html


def test_render_policy_page_escapes_obligation_summary() -> None:
    """验证义务概括里的 HTML 也会被转义。

    义务内容是人工撰写的长文本，比标题更容易混入尖括号
    （例如「用户 <100 万」这类写法）。
    """
    # 构造带尖括号的义务
    from finreg_ai.models import KeyObligation  # 局部导入：仅本测试需要

    # 政策
    policy = make_policy(
        key_obligations=[KeyObligation(clause="第三条", summary="注册用户 <100 万时需报告")],
    )
    # 渲染
    page = render_policy_page(policy, "2026-10-06 12:00 +0800")
    # 裸尖括号不应出现
    assert "<100" not in page
    # 应转义
    assert "&lt;100" in page


# ============================================================
# 空数据与异常数据
# ============================================================

def test_build_site_handles_empty_repository(tmp_path: Path) -> None:
    """验证库中什么都没有时也能正常构建。

    「还没有收录任何政策」是合法状态，不是错误。
    把空库当错误会逼人塞一条凑数记录进去——本项目已在
    `data/enforcement/` 的「允许为空」上确立过同样的原则。
    """
    # 空库构建
    result, out = build_into(tmp_path)
    # 应正常产出
    assert result.policy_count == 0
    # 页面数 = 首页 + 变更流
    assert result.page_count == 2
    # 首页应给出空状态说明，而不是一片空白
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 空状态文案
    assert "还没有政策记录" in index_html
    # 变更流页同样给出说明
    changes_html = (out / "changes.html").read_text(encoding="utf-8")
    # 空状态文案
    assert "尚无变更记录" in changes_html


def test_build_site_records_unreadable_change_file_as_problem(tmp_path: Path) -> None:
    """验证损坏的变更文件被记为问题，而不是静默跳过。

    「读不出来」与「当天没有变化」在页面上必须能区分。
    这正是本项目在 `pipeline.write_change_set` 里处理同一个文件时
    遵循的原则——只是方向相反：那里保护写入，这里保证可见。
    """
    # 构造一份顶层不是对象的变更文件
    broken = {"_unreadable": "2026-10-01.json", "_error": "Expecting value: line 1", "changes": []}
    # 构建
    result, out = build_into(tmp_path, change_documents=[broken])
    # 必须产生构建问题
    assert any("2026-10-01.json" in p for p in result.problems)
    # 变更流页必须显式报警，而不是显示「没有变化」
    changes_html = (out / "changes.html").read_text(encoding="utf-8")
    # 警示文案
    assert "无法解析" in changes_html
    # 必须说清这不是「当天没有变化」
    assert "这不是「当天没有变化」" in changes_html


def test_build_site_records_missing_asset_directory_as_problem(tmp_path: Path) -> None:
    """验证静态资源目录缺失时给出构建问题。

    CSS 缺失只会让页面变丑、不会丢内容，因此不该阻断构建；
    但也不能静默——「样式没了」在构建日志里必须留下痕迹。
    """
    # 指向一个不存在的站点源目录
    result, _out = build_into(tmp_path, site_source_dir=tmp_path / "不存在的目录")
    # 应记录问题
    assert any("静态资源目录不存在" in p for p in result.problems)


def test_build_site_only_counts_record_issues_when_loading_from_disk(tmp_path: Path) -> None:
    """验证注入政策时不产生「记录校验提示」计数。

    这个字段表达的是「从磁盘读数据时发现了多少条校验提示」。
    注入了数据就没有读盘动作，计数理应为 0——
    否则会出现「明明没读文件，却报告有校验提示」这种自相矛盾。
    """
    # 注入一条政策（不读盘）
    result, _out = build_into(tmp_path, policies={p.id: p for p in [make_policy()]})
    # 计数应为 0
    assert result.record_issue_count == 0


# ============================================================
# JSON API 与 schema 的一致性
# ============================================================

def test_policies_json_top_level_matches_schema(tmp_path: Path) -> None:
    """验证 JSON API 里每条记录的顶层字段符合 policy.schema.json。

    这是本条 API 能被机器消费的前提：消费者应当能直接拿 schema 去校验它。
    派生字段刻意收在 ``derived`` 子对象里，正是为了不破坏这一点——
    若把它们平铺进顶层，schema 的 additionalProperties: false 会立刻报错。
    """
    # 录一条内容尽量完整的政策。
    # domain 必须用 schema 受控词表里的中文取值：
    # test_models 的工厂默认给的是 "banking"，那是为了测业务规则而非 schema，
    # 用在这里会让校验因为「取值不在词表内」而失败，把测试带偏到无关的方向。
    # issuer_code 同理必须给出——schema 里它不是必填项，但一旦出现在记录里
    # 就必须是字符串，不能是 None。真实数据因为要过 validate 闸门，必定填了它。
    policy = make_policy(domain=["银行", "人工智能"], issuer_code="nfra")
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 解析 API
    api = json.loads((out / "data" / "policies.json").read_text(encoding="utf-8"))
    # 取第一条
    record = api["policies"][0]
    # 剥离派生子对象后，其余字段必须通过 schema 校验
    stripped = {k: v for k, v in record.items() if k != "derived"}
    # 执行校验
    problems = validate_against_schema(stripped, load_policy_schema())
    # 不应有任何校验问题
    assert problems == [], f"JSON API 记录不符合 schema：{problems}"


def test_policies_json_derived_block_exposes_view_properties(tmp_path: Path) -> None:
    """验证派生块给出了中文状态标签与详情页地址。"""
    # 现行有效的政策
    policy = make_policy()
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 解析
    api = json.loads((out / "data" / "policies.json").read_text(encoding="utf-8"))
    # 取派生块
    derived = api["policies"][0]["derived"]
    # 中文标签
    assert derived["status_label"] == "现行有效"
    # 有效性判定
    assert derived["is_currently_valid"] is True
    # 详情页地址
    assert derived["page"] == f"policies/{policy.id}.html"


def test_policies_json_derived_marks_repealed_as_not_valid(tmp_path: Path) -> None:
    """验证「已废止」在派生层被正确判为无效。

    反向用例：若 is_currently_valid 被写死成 True，上一条测试仍会通过，
    只有这一条会红。两条一起构成夹逼。
    """
    # 已废止的政策
    policy = make_policy(status=PolicyStatus.REPEALED)
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 解析
    api = json.loads((out / "data" / "policies.json").read_text(encoding="utf-8"))
    # 取派生块
    derived = api["policies"][0]["derived"]
    # 不应被判为有效
    assert derived["is_currently_valid"] is False
    # 标签应为「已废止」
    assert derived["status_label"] == "已废止"


# ============================================================
# 状态徽章的语义
# ============================================================

@pytest.mark.parametrize(
    ("status", "expected_class"),
    [
        (PolicyStatus.EFFECTIVE, "badge-ok"),            # 有效 → 有效色
        (PolicyStatus.PARTIALLY_EFFECTIVE, "badge-ok"),  # 部分有效也算有效
        (PolicyStatus.CONSULTATION, "badge-pending"),    # 未生效 → 待生效色
        (PolicyStatus.REPEALED, "badge-dead"),           # 已废止 → 失效色
        (PolicyStatus.UNKNOWN, "badge-warn"),            # 状态不明 → 警示色
    ],
)
def test_status_badge_tone_matches_status_semantics(status: PolicyStatus, expected_class: str) -> None:
    """验证每种状态落到正确的视觉色调。

    「已废止」与「征求意见」都必须与「现行有效」区分开，且两者之间也要区分：
    前者意味着「不要再据此办事」，后者意味着「现在还不能据此办事」，
    处置方向完全相反，用同一种颜色会误导读者。
    """
    # 构造该状态的政策
    policy = make_policy(status=status)
    # 渲染详情页
    page = render_policy_page(policy, "2026-10-06 12:00 +0800")
    # 应出现预期色调的徽章类名
    assert expected_class in page


def test_status_badge_warn_is_distinct_from_dead() -> None:
    """验证「状态待核验」与「已废止」用的是不同色调。

    反向用例：若两者都映射成中性色，上面那组参数化测试仍可能通过
    （只要 dead 用中性、warn 用别的），但这条会红。
    """
    # 待核验
    unknown_page = render_policy_page(make_policy(status=PolicyStatus.UNKNOWN), "2026-10-06 12:00 +0800")
    # 已废止
    repealed_page = render_policy_page(make_policy(status=PolicyStatus.REPEALED), "2026-10-06 12:00 +0800")
    # 两者不应使用同一个色调类
    assert "badge-warn" in unknown_page
    # 已废止用的是中性色，不应是警示色
    assert "badge-warn" not in repealed_page


# ============================================================
# 生效日期的三种状态
# ============================================================

def test_policy_page_distinguishes_three_effective_date_states() -> None:
    """验证生效日期一栏能区分三种状态。

    这是列表页踩过的一个显示坑（见 cli.py 的注释）：
    「有日期」「尚未生效」「现行有效但日期待核验」三态，
    若一律显示成「未生效」，一条**现行有效**的政策会被读成「还没生效」——
    这是会被误读的严重显示错误。
    三态一起断言，形成夹逼：任何两态合并都会让其中一条断言失败。
    """
    # 一、有明确生效日期
    dated = render_policy_page(
        make_policy(status=PolicyStatus.EFFECTIVE, effective_from=datetime(2026, 7, 1).date()),
        "2026-10-06 12:00 +0800",
    )
    # 应直接显示日期
    assert "2026-07-01" in dated
    # 不应出现待核验标记
    assert "待核验" not in dated

    # 二、现行有效但日期缺失 → 必须标注待核验，并指向核验台账
    missing = render_policy_page(
        make_policy(status=PolicyStatus.EFFECTIVE, effective_from=None),
        "2026-10-06 12:00 +0800",
    )
    # 应出现待核验
    assert "待核验" in missing
    # 并说清原因
    assert "施行日期尚未确认" in missing

    # 三、确实尚未生效 → 用「尚未生效」，不能与「待核验」混用
    not_yet = render_policy_page(
        make_policy(status=PolicyStatus.CONSULTATION, effective_from=None),
        "2026-10-06 12:00 +0800",
    )
    # 应显示尚未生效
    assert "尚未生效" in not_yet
    # 不应说成待核验——征求意见稿本来就没有生效日期，这不是「没查到」
    assert "待核验" not in not_yet


# ============================================================
# 检索范围
# ============================================================

def test_index_search_blob_covers_obligation_text(tmp_path: Path) -> None:
    """验证首页检索范围覆盖义务正文，而不只是标题。

    这是本项目的核心增值点：使用者输入「提示词注入」时，
    应该能命中那条义务里写了提示词注入、但标题完全没提的政策。
    若匹配范围退回标题，这个能力会静默消失，而页面看起来毫无异常。
    """
    # 局部导入：仅本测试需要
    from finreg_ai.models import KeyObligation

    # 标题完全不含关键词，义务里含
    policy = make_policy(
        id="test-2026-search-demo",
        title="关于某项管理工作的指导意见",
        key_obligations=[KeyObligation(clause="第十二条", summary="须防范提示词注入与上下文污染")],
    )
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 读首页
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 关键词必须出现在 data-search 属性里，检索脚本才有得可搜
    assert "提示词注入" in index_html


def test_index_search_blob_tolerates_missing_issuer_code(tmp_path: Path) -> None:
    """验证机构代码为空时不会让构建崩溃。

    这是一个真实缺陷的回归测试：`issuer_code` 在模型里是可选字段，
    早期实现直接把它 join 进检索文本，遇到 None 会抛 TypeError，
    整个站点构建因此失败——「一个可选字段没填」不该是这个后果。
    """
    # 显式把机构代码置空
    policy = make_policy(id="test-2026-no-code", issuer_code=None)
    # 构建不应抛异常
    result, _out = build_into(tmp_path, policies={policy.id: policy})
    # 应正常产出
    assert result.policy_count == 1
    # 机构筛选属性渲染成空串，而不是字面量 None
    index_html = (_out / "index.html").read_text(encoding="utf-8")
    # 不能出现 None 字面量
    assert 'data-issuer="None"' not in index_html


def test_index_search_blob_covers_issuer_code(tmp_path: Path) -> None:
    """验证检索范围也覆盖机构代码，使「nfra」这类英文缩写可搜。"""
    # 构造政策：显式给出机构代码（工厂默认不含该字段）
    policy = make_policy(id="test-2026-issuer-demo", issuer_code="nfra")
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 读首页
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 机构代码应进入检索文本，而不只是进筛选属性
    assert 'data-issuer="nfra"' in index_html
    # 且出现在 data-search 中（前后有分隔空格）
    assert " nfra " in index_html


# ============================================================
# 相对路径（子目录页面）
# ============================================================

def test_detail_page_uses_parent_prefix_for_assets(tmp_path: Path) -> None:
    """验证详情页的资源与导航链接都带 ``../`` 前缀。

    这是最容易出错、又最容易被漏测的一处：少一个 ``../`` 时，
    首页完全正常、只有详情页整体丢样式。人手动点开首页检查是发现不了的。
    """
    # 构造政策
    policy = make_policy()
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 读详情页
    detail = (out / "policies" / f"{policy.id}.html").read_text(encoding="utf-8")
    # 样式路径必须回到上一级
    assert 'href="../assets/style.css"' in detail
    # 脚本同理
    assert 'src="../assets/app.js"' in detail
    # 返回首页的链接
    assert 'href="../index.html"' in detail
    # 首页则**不应**有 ../ 前缀（反向夹逼：若把前缀写死成 ../，这条会红）
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 首页用根相对路径
    assert 'href="assets/style.css"' in index_html
    # 首页不该出现返回上一级的资源引用
    assert 'href="../assets/style.css"' not in index_html


def test_every_page_links_favicon_with_correct_prefix(tmp_path: Path) -> None:
    """验证首页与详情页都声明了 favicon，且子目录页面带 ``../`` 前缀。

    favicon 缺失时浏览器会给每个页面请求一次 /favicon.ico 并拿到 404——
    这不会报错，但会让站点在浏览器标签页上缺少标识，属于「看不见的不完整」。
    与样式表同理，详情页的图标链接最容易因为少了 ``../`` 而静默 404。
    """
    # 一条政策，构建出首页与详情页
    policy = make_policy()
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 首页用根相对路径
    index_html = (out / "index.html").read_text(encoding="utf-8")
    # 首页的图标链接
    assert 'rel="icon" href="assets/favicon.ico"' in index_html
    # 详情页必须回到上一级
    detail = (out / "policies" / f"{policy.id}.html").read_text(encoding="utf-8")
    # 详情页的图标链接
    assert 'rel="icon" href="../assets/favicon.ico"' in detail
    # 反向夹逼：首页若被加上 ../ 前缀，此断言会红
    assert 'rel="icon" href="../assets/favicon.ico"' not in index_html


# ============================================================
# 变更文件加载
# ============================================================

def test_load_change_documents_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """验证目录不存在时返回空列表而不是抛异常。"""
    # 指向一个不存在的目录
    assert load_change_documents(tmp_path / "没有这个目录") == []


def test_load_change_documents_sorts_days_descending(tmp_path: Path) -> None:
    """验证变更文件按日期倒序返回，最新的在最前。"""
    # 建目录
    changes_dir = tmp_path / "changes"
    # 一次性建出
    changes_dir.mkdir(parents=True, exist_ok=True)
    # 写入三个日期，文件名故意不按顺序创建
    for day in ("2026-10-03", "2026-10-05", "2026-10-04"):
        # 写入最小合法内容
        (changes_dir / f"{day}.json").write_text(
            json.dumps(make_change_document(day=day), ensure_ascii=False), encoding="utf-8"
        )
    # 加载
    documents = load_change_documents(changes_dir)
    # 日期顺序应为倒序
    assert [d["detected_on"] for d in documents] == ["2026-10-05", "2026-10-04", "2026-10-03"]


def test_load_change_documents_marks_corrupt_file_instead_of_raising(tmp_path: Path) -> None:
    """验证损坏的 JSON 被标成不可读记录，而不是抛异常中断加载。

    一条变更文件坏掉，不该让整个站点构建失败——
    但也不能被静默丢掉，否则「文件坏了」会被读成「那天没有变化」。
    """
    # 建目录
    changes_dir = tmp_path / "changes"
    # 建出目录
    changes_dir.mkdir(parents=True, exist_ok=True)
    # 写一个非法 JSON
    (changes_dir / "2026-10-01.json").write_text("{ 这不是 JSON", encoding="utf-8")
    # 再写一个合法 JSON 但顶层是列表
    (changes_dir / "2026-10-02.json").write_text("[1, 2, 3]", encoding="utf-8")
    # 加载：不应抛出
    documents = load_change_documents(changes_dir)
    # 两条都应被标记为不可读
    assert len(documents) == 2
    # 全部带 _unreadable 标记
    assert all(d.get("_unreadable") for d in documents)
    # 第二条的错误说明应指出顶层不是对象，而不是笼统的「解析失败」
    second = next(d for d in documents if d["_unreadable"] == "2026-10-02.json")
    # 明确的原因
    assert second["_error"] == "顶层不是 JSON 对象"


def test_load_change_documents_ignores_non_json_files(tmp_path: Path) -> None:
    """验证 .gitkeep 之类的非 JSON 文件不会被当成变更文件读进来。

    `data/changes/` 目录里确实有一个 `.gitkeep`——
    若用 ``iterdir()`` 而不是 ``glob("*.json")``，它会以「解析失败」的姿态
    出现在构建问题里，制造一个永远不会消失的假告警。
    假告警会让人开始忽略告警区，比不告警更糟。
    """
    # 建目录
    changes_dir = tmp_path / "changes"
    # 建出
    changes_dir.mkdir(parents=True, exist_ok=True)
    # 写一个占位文件
    (changes_dir / ".gitkeep").write_text("", encoding="utf-8")
    # 写一个正常的变更文件
    (changes_dir / "2026-10-05.json").write_text(
        json.dumps(make_change_document(), ensure_ascii=False), encoding="utf-8"
    )
    # 加载
    documents = load_change_documents(changes_dir)
    # 只应有那一条 JSON
    assert len(documents) == 1
    # 且没有不可读标记
    assert not documents[0].get("_unreadable")


# ============================================================
# 变更流页
# ============================================================

def test_changes_page_renders_change_type_label_in_chinese() -> None:
    """验证变更类型被翻译成中文，而不是直接显示枚举值。"""
    # 渲染
    page = render_changes_page([make_change_document(changes=[make_change()])], "2026-10-06 12:00 +0800")
    # 中文标签
    assert "待录入" in page
    # 不应把内部枚举值直接暴露给读者
    assert ">discovered<" not in page


def test_changes_page_states_that_machine_finds_are_not_yet_human_verified() -> None:
    """验证变更流页明确说明「机器发现 ≠ 人工认定」。

    这条界线是本项目最重要的一条数据纪律。若页面上不说清，
    读者会把「待录入」当成「已收录的政策」——那等于把机器的一次关键词命中
    直接升格成了合规事实。
    """
    # 渲染
    page = render_changes_page([make_change_document(changes=[make_change()])], "2026-10-06 12:00 +0800")
    # 必须写明尚待人工认定
    assert "尚未经人工认定" in page
    # 必须写明这条纪律
    assert "机器负责发现，人负责认定" in page


def test_changes_page_escapes_change_title() -> None:
    """验证变更标题里的 HTML 被转义。

    变更标题来自抓取到的官方页面标题，是**外部输入**，
    比人工维护的字段更需要防范。
    """
    # 构造带标签的变更
    change = make_change(title="<img src=x onerror=alert(1)> 某标题")
    # 渲染
    page = render_changes_page([make_change_document(changes=[change])], "2026-10-06 12:00 +0800")
    # 原始标签不得出现
    assert "<img src=x" not in page
    # 应被转义
    assert "&lt;img" in page


# ============================================================
# 免责声明
# ============================================================

def test_every_page_carries_the_disclaimer(tmp_path: Path) -> None:
    """验证免责声明出现在**每一个**页面上，而不只是首页。

    使用者从搜索引擎直接落到某条政策详情页时不会经过首页。
    合规类内容的免责声明如果只在首页，等于对最主要的那批访问者没有生效。
    """
    # 一条政策
    policy = make_policy()
    # 构建
    _result, out = build_into(
        tmp_path,
        policies={policy.id: policy},
        change_documents=[make_change_document(changes=[make_change()])],
    )
    # 逐页检查
    for relative in ("index.html", "changes.html", f"policies/{policy.id}.html"):
        # 读取
        page = (out / relative).read_text(encoding="utf-8")
        # 必须含免责声明
        assert "不构成法律意见" in page, f"页面缺少免责声明：{relative}"


def test_policy_page_links_to_official_source(tmp_path: Path) -> None:
    """验证详情页给出官方原文链接，且带安全的外链属性。

    没有官方链接的合规记录是不可核验的，整条记录的价值归零。
    外链属性则是纯防御性的零成本措施。
    """
    # 构造政策
    policy = make_policy()
    # 构建
    _result, out = build_into(tmp_path, policies={policy.id: policy})
    # 读详情页
    detail = (out / "policies" / f"{policy.id}.html").read_text(encoding="utf-8")
    # 官方链接
    assert "https://example.gov.cn/a.html" in detail
    # 必须带 noopener
    assert 'rel="noopener noreferrer"' in detail


def test_policy_page_warns_when_official_url_missing(tmp_path: Path) -> None:
    """验证缺少官方链接时给出显式提示，而不是渲染一个点不开的空链接。

    这正是本项目「`source.url` 必须指向文件本身」那条待办的可见化：
    让缺口在页面上就能被看见，而不是靠人去翻 YAML 才发现。
    """
    # 局部导入：构造一条官方链接为空的溯源信息
    from finreg_ai.models import SourceRef

    # 链接置空的溯源
    blank_source = SourceRef(
        url="",
        site="example.gov.cn",
        fetched_at="2026-10-03T12:00:00+08:00",
    )
    # 构造政策
    policy = make_policy(source=blank_source)
    # 渲染
    page = render_policy_page(policy, "2026-10-06 12:00 +0800")
    # 必须有显式提示
    assert "（无官方链接）" in page
