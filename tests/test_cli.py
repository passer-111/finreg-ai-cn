"""命令行层测试 —— 验证退出码与输出格式，全程离线。

为什么 CLI 值得测
----------------
本项目的 CLI 有两个身份：给人用的运维工具，以及 CI 的判据。
第二重身份意味着**退出码是契约**：``0`` 表示数据可信，非 ``0`` 表示
必须有人看一眼。若退出码写错，CI 会变成永远绿灯的摆设。
因此这里对每个子命令都断言具体退出码，而不只是「没抛异常」。

离线保证：``validate`` / ``list`` / ``show`` / ``stale`` 只读本地文件；
``fetch`` 只针对人工录入源执行，不产生任何网络请求。
"""

# 导入 json 用于解析 JSON 输出
import json

# 导入 pytest
import pytest

# 导入 CLI 入口与退出码常量
from finreg_ai.cli import EXIT_ISSUES, EXIT_OK, main
# 导入数据加载函数以便动态取得一条真实记录 id
from finreg_ai.store import load_all_policies


# ============================================================
# 基础行为
# ============================================================

def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 --version 打印版本号并以退出码 0 结束。"""
    # argparse 的 version 动作通过抛 SystemExit 结束
    with pytest.raises(SystemExit) as excinfo:
        # 触发版本输出
        main(["--version"])
    # 退出码应为 0
    assert excinfo.value.code == 0
    # 输出中应包含项目名
    assert "finreg-ai-cn" in capsys.readouterr().out


def test_missing_subcommand_exits_with_usage_error() -> None:
    """验证未指定子命令时以用法错误码（2）结束。"""
    # 不带任何子命令
    with pytest.raises(SystemExit) as excinfo:
        # argparse 会在缺少必需子命令时报错退出
        main([])
    # argparse 约定用法错误退出码为 2
    assert excinfo.value.code == 2


# ============================================================
# validate —— CI 门禁
# ============================================================

def test_validate_returns_ok_when_repository_is_clean(capsys: pytest.CaptureFixture[str]) -> None:
    """验证仓库无错误时 validate 返回 0。"""
    # 执行校验子命令
    code = main(["validate"])
    # 断言退出码
    assert code == EXIT_OK
    # 输出应包含提示信息（无论是通过还是有警告）
    out = capsys.readouterr().out
    # 断言有输出
    assert out.strip() != ""


def test_validate_reports_warning_section_separately(capsys: pytest.CaptureFixture[str]) -> None:
    """验证警告被单独成段展示，不与错误混在一起。

    混在一起的后果：维护者看到一片红，会倾向于直接删掉那些提示，
    把「可见的待办」变成「不可见的技术债」。
    """
    # 执行校验
    main(["validate"])
    # 取输出
    out = capsys.readouterr().out
    # 若存在警告，标题中应明确说明「不阻断」
    if "待办提示" in out:
        # 断言措辞
        assert "不阻断" in out


def test_validate_sources_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """验证数据源登记表校验通过并返回 0。"""
    # 执行校验
    code = main(["validate-sources"])
    # 断言退出码
    assert code == EXIT_OK
    # 输出应包含机构与源的数量统计
    out = capsys.readouterr().out
    # 断言统计信息存在
    assert "个机构" in out


# ============================================================
# list —— 检索
# ============================================================

def test_list_json_output_is_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 list --json 输出的是合法 JSON，可被程序消费。"""
    # 执行列出命令
    code = main(["list", "--json"])
    # 退出码应为 0
    assert code == EXIT_OK
    # 解析输出
    payload = json.loads(capsys.readouterr().out)
    # 应为列表
    assert isinstance(payload, list)
    # 至少有一条记录
    assert len(payload) >= 1
    # 每条记录都应含关键字段
    assert {"id", "title", "status", "url"} <= set(payload[0])


def test_list_effective_only_returns_only_valid_records(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 --effective-only 只返回现行有效的记录。"""
    # 执行过滤列出
    code = main(["list", "--effective-only", "--json"])
    # 退出码
    assert code == EXIT_OK
    # 解析
    payload = json.loads(capsys.readouterr().out)
    # 允许的有效状态
    allowed = {"effective", "partially_effective", "amended"}
    # 逐条断言
    for record in payload:
        # 状态必须在允许集合中
        assert record["status"] in allowed


def test_list_filter_by_issuer(capsys: pytest.CaptureFixture[str]) -> None:
    """验证按机构代码筛选生效。"""
    # 执行筛选
    code = main(["list", "--issuer", "cac", "--json"])
    # 退出码
    assert code == EXIT_OK
    # 解析
    payload = json.loads(capsys.readouterr().out)
    # 结果非空
    assert len(payload) >= 1
    # 每条都应属于网信办
    for record in payload:
        # 断言机构前缀
        assert record["id"].startswith("cac-")


def test_list_with_no_match_returns_ok_not_error(capsys: pytest.CaptureFixture[str]) -> None:
    """验证「无结果」不是错误，退出码仍为 0。

    这是刻意的语义区分：查询没命中是正常结果，
    与「数据损坏」必须用不同的退出码表达。
    """
    # 用一个不存在的机构代码筛选
    code = main(["list", "--issuer", "no-such-issuer"])
    # 应为成功
    assert code == EXIT_OK
    # 输出应给出明确提示
    assert "没有符合条件" in capsys.readouterr().out


# ============================================================
# show —— 查看单条
# ============================================================

def _first_policy_id() -> str:
    """取得库中第一条政策记录的 id，避免测试写死具体文件名。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 取任意一条的 id
    return next(iter(policies))


def test_show_displays_policy_details(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 show 能渲染出记录的关键信息。"""
    # 取一条真实记录
    policy_id = _first_policy_id()
    # 执行查看
    code = main(["show", policy_id])
    # 退出码
    assert code == EXIT_OK
    # 取输出
    out = capsys.readouterr().out
    # 应包含标题、状态、官方链接等关键栏目
    assert "标题：" in out
    # 状态
    assert "状态：" in out
    # 官方链接——可核验性的体现
    assert "官方链接：" in out


def test_show_unknown_policy_returns_issues_code(capsys: pytest.CaptureFixture[str]) -> None:
    """验证查询不存在的记录返回非零退出码，并把提示写到 stderr。"""
    # 执行查看
    code = main(["show", "definitely-not-a-real-policy-id"])
    # 应为问题码
    assert code == EXIT_ISSUES
    # 错误提示应写到 stderr，避免污染正常输出
    assert "未找到" in capsys.readouterr().err


def test_show_json_output_is_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 show --json 输出合法 JSON。"""
    # 取一条真实记录
    policy_id = _first_policy_id()
    # 执行查看
    code = main(["show", policy_id, "--json"])
    # 退出码
    assert code == EXIT_OK
    # 解析并断言关键字段存在
    payload = json.loads(capsys.readouterr().out)
    # 标识一致
    assert payload["id"] == policy_id
    # 含版本链
    assert isinstance(payload["versions"], list)


# ============================================================
# stale —— 时效性治理
# ============================================================

# 仓库内全部政策记录的最近核验日期。
# 【为什么把它写成常量而不是在测试里取 today()】
# stale 的判定依赖「基准日 - 核验日 > 阈值」。若测试隐含地以「今天」
# 作基准，那么只要仓库数据不天天更新，天数就会逐日增长，
# 断言迟早失效——这与代码正确性无关，纯属测试设计缺陷。
# 把核验日固定成常量并显式传给 --as-of，测试就与运行日期彻底解耦。
# 代价是：当贡献者批量更新核验台账后，这个常量需要同步更新。
# 这个代价是可接受的——它会以「明确的一条测试失败」暴露出来，
# 而不是让整片测试随时间悄悄变红。
_VERIFIED_ON = "2026-10-03"

# 由 _VERIFIED_ON 向后推 31 天得到的基准日，用于验证 --as-of 确实生效。
# 【为什么是 31 天而不是 30 天】判定条件是「天数 > 阈值」。
# 若取 30 天、阈值也取 30，则 30 > 30 为假，记录不会被列出，
# 测试会误以为 --as-of 没生效。多推一天才能越过这个边界。
# 这个边界由另一条测试（--days 0 + 基准日=核验日）从反面覆盖。
_THIRTY_ONE_DAYS_LATER = "2026-11-03"


def test_stale_with_huge_threshold_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """验证阈值极大时没有陈旧记录，退出码仍为 0。

    注意：陈旧记录是**待办事项**而非错误，因此不应导致非零退出码。
    这一点很重要——否则 CI 会因为「数据该复核了」而永久变红，
    结果就是没人再看它。
    """
    # 使用一个极大的阈值
    code = main(["stale", "--days", "100000"])
    # 应为成功
    assert code == EXIT_OK
    # 输出应说明没有超期记录
    assert "没有超过" in capsys.readouterr().out


def test_stale_with_negative_threshold_lists_records(capsys: pytest.CaptureFixture[str]) -> None:
    """验证阈值设为 -1 时能列出记录，且每条都给出官方链接便于核对。

    为什么用 -1 而不是 0：判定条件是「天数 > 阈值」，
    而当天核验过的记录天数为 0，用阈值 0 时 0 > 0 为假，不会列出。
    用 -1 才能覆盖「包含刚核验过的记录」这一边界。
    """
    # 阈值为 -1，所有记录（含当天核验的）都超期
    code = main(["stale", "--days", "-1"])
    # 仍为成功退出码
    assert code == EXIT_OK
    # 取输出
    out = capsys.readouterr().out
    # 应列出记录
    assert "条记录超过" in out
    # 应给出官方链接，便于直接点开核对
    assert "http" in out


def test_stale_day_zero_excludes_records_verified_today(capsys: pytest.CaptureFixture[str]) -> None:
    """验证阈值为 0 时，当天核验过的记录不算超期。

    这是「>」而非「>=」的语义确认：刚核验完的记录天数为 0，
    若把它判为超期，会让人对工具的判断力失去信任。

    【为什么必须传 --as-of】
    本用例最初写成「直接跑 ``stale --days 0``，断言没有超期项」，
    理由是「当前库中记录均为近期核验」。这个理由在写下它的当天成立，
    但它把断言绑死在**真实数据的核验日期**上：核验台账一变成两天前，
    天数就成了 2，断言立刻失败——而代码一行没改。

    这正是本项目最敌视的那类缺陷：**随时间自行变红的测试**。
    它比没有测试更糟，因为红过一次之后，人们就开始习惯性忽略红灯，
    校验闸门随之失效。修法是把「当天」变成显式输入，而不是靠巧合。

    实现上不选 monkeypatch 而选给 CLI 加 ``--as-of`` 参数：
    参数是**生产代码里真实存在的能力**（运维可用它预演未来某天的待核验队列），
    而 monkeypatch 只是测试的脚手架。测一个真实存在的旋钮，
    比测「我替换掉的某个内部函数」更接近用户实际会走的路径。
    """
    # 先把基准日钉在仓库里记录的核验日，使「当天核验」成为确定事实
    code = main(["stale", "--days", "0", "--as-of", _VERIFIED_ON])
    # 成功退出码
    assert code == EXIT_OK
    # 取输出
    out = capsys.readouterr().out
    # 基准日即核验日，天数为 0，0 > 0 为假，因此不应有超期项
    assert "没有超过 0 天未核验的记录" in out


def test_stale_as_of_shifts_days_forward(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 --as-of 能把基准日推到未来，从而让原本未超期的记录变为超期。

    这条测试覆盖的是「预演」能力：运维想提前知道再过一个月
    哪些记录会进入待核验队列，无需真的等到那天。
    同时它也构成对 ``test_stale_day_zero_excludes_records_verified_today``
    的反向验证——如果 --as-of 被静默忽略，那一条会通过而这一条会失败，
    两条一起才能把「基准日确实生效」夹住。
    """
    # 把基准日推到核验日之后 31 天，此时每条记录都已核验满 31 天
    code = main(["stale", "--days", "30", "--as-of", _THIRTY_ONE_DAYS_LATER])
    # 成功退出码——陈旧记录属于待办而非错误，不改变退出码
    assert code == EXIT_OK
    # 取输出
    out = capsys.readouterr().out
    # 31 天 > 阈值 30，因此必然列出记录
    assert "30 天未核验" in out
    # 应显式回显基准日，否则用户看到的天数会与「今天」对不上而怀疑算错
    assert _THIRTY_ONE_DAYS_LATER in out


def test_stale_as_of_rejects_malformed_date(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 --as-of 收到非法日期时以用法错误码（2）结束。

    为什么关心退出码而不是只关心「报错了」：
    2 表示「用户把参数敲错了」，1 表示「数据有问题」。
    CI 与脚本会据此分流——前者该改命令行，后者该去看数据。
    若两者混为同一个码，自动化就没法正确处置。
    """
    # 传入一个不存在的日期，argparse 应在解析阶段就拒绝
    with pytest.raises(SystemExit) as excinfo:
        # 2026-02-30 不是合法日历日期
        main(["stale", "--days", "30", "--as-of", "2026-02-30"])
    # argparse 约定用法错误退出码为 2
    assert excinfo.value.code == 2
    # 错误信息应出现在 stderr 且指明格式期望
    captured = capsys.readouterr()
    # 断言提示里包含正确格式，便于用户自查
    assert "YYYY-MM-DD" in captured.err


# ============================================================
# fetch —— 针对人工录入源执行的离线流水线
# ============================================================

def test_fetch_for_manual_source_runs_offline_and_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """验证针对人工录入源的流水线可完全离线跑通。

    选择 manual-entry 是刻意的：它在设计上就不做网络请求，
    因此这条测试既能覆盖流水线与报告生成的全链路，
    又不会给任何政府网站发请求，也不会因外部站点故障而变红。
    """
    # 只抓人工录入源，且不写盘
    code = main(["fetch", "--source", "manual-entry", "--dry-run"])
    # 应为成功
    assert code == EXIT_OK
    # 取输出
    out = capsys.readouterr().out
    # 应包含抓取完成的标题
    assert "抓取完成" in out
    # 应显示该源被跳过
    assert "manual-entry" in out
    # 应说明跳过原因是人工录入源
    assert "人工录入源" in out


def test_fetch_json_report_contains_summary(capsys: pytest.CaptureFixture[str]) -> None:
    """验证 fetch --json 输出结构化报告，且包含概要统计。"""
    # 执行
    code = main(["fetch", "--source", "manual-entry", "--dry-run", "--json"])
    # 退出码
    assert code == EXIT_OK
    # 解析
    payload = json.loads(capsys.readouterr().out)
    # 应含运行日期
    assert "run_on" in payload
    # 应含概要
    assert "summary" in payload
    # 概要应含失败源清单——这一项必须存在，否则「无变更」会被误读为「一切正常」
    assert "failed_sources" in payload["summary"]
    # 应含各源明细
    assert isinstance(payload["fetch_results"], list)
