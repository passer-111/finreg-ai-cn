"""仓库层测试 —— 对**真实数据文件**做完整性校验。

为什么这一层测试特别重要
------------------------
其它测试用的是合成数据，验证的是「代码逻辑对不对」。
这一层直接读取仓库里的 ``data/policies/*.yaml`` 与 ``data/sources.yaml``，
验证的是「**实际要发布的数据是否站得住**」。

它同时也是 CI 的守门人：任何一次提交若引入结构错误、悬空的版本链、
或未登记的机构代码，都会在这里失败。因此这批测试必须完全离线——
CI 不应该因为某个政府网站今天恰好宕机而变红。
"""

# 导入 json 用于验证导出字典可序列化
import json

# 导入 date 类型用于注入基准日期
from datetime import date

# 导入 pathlib 用于路径断言
from pathlib import Path

# 导入 pytest
import pytest

# 导入 yaml 用于在临时目录里造执法记录文件
import yaml

# 导入待测的仓库层函数
from finreg_ai.pipeline import split_issues, validate_repository
from finreg_ai.models import enforcement_from_dict, policy_to_dict
from finreg_ai.store import (
    ENFORCEMENT_DIR,
    POLICIES_DIR,
    SOURCES_FILE,
    compute_hash,
    find_stale_policies,
    load_all_enforcement,
    load_all_policies,
    load_enforcement_file,
    load_sources,
    normalize_text,
    save_enforcement,
    validate_cross_references,
    validate_enforcement_references,
)

# 从抓取器包导入注册表，用于校验「配置声明的 fetcher 都有实现」
from finreg_ai.fetchers import _FETCHER_REGISTRY


# ============================================================
# 数据源登记表
# ============================================================

def test_sources_registry_loads_without_errors() -> None:
    """验证数据源登记表能被加载，且无结构性问题。"""
    # 加载登记表
    _issuers, _sources, errors = load_sources()
    # 不应有任何加载错误
    assert errors == []


def test_sources_registry_has_expected_scale() -> None:
    """验证登记表中的机构与源数量符合当前规划。

    数字写死是刻意的：它会让「不小心删掉一个机构定义」变成一次测试失败，
    而不是一个静默的数据缺口。

    2026-10-05 变更：数据源由 10 增至 12。原因是网信办源被拆分——
    早期把网信办记为一个源且判断其不可达，复核后发现「不可达」实为 URL 失效，
    且网信办把不同类型的文件分列在不同栏目下（部门规章 / 规范性文件 / 政策文件），
    三栏目内容互补（分别命中 AI 专项规章、金融交叉规范、国家级 AI 产业政策），
    因此拆为三个独立源。这不是简单加数量，而是修正了一个覆盖盲区。

    2026-10-05 再变更：数据源由 12 增至 13，新增人民银行「行政处罚公示」。
    它属于**另一类数据**（执法记录，而非政策文件），默认 ``enabled: false``——
    罚单每次数百条，进每日变更流会淹没真正的政策变化，因此按需抓取。
    数量写死在这里的意义不只是防手滑删定义，也让人一眼看到
    「本项目现在同时治理政策与执法两类数据」。
    """
    # 加载登记表
    issuers, sources, _errors = load_sources()
    # 当前登记了 14 个机构
    assert len(issuers) == 14
    # 当前登记了 13 个数据源（网信办按栏目拆为 3 个，另有 1 个行政处罚源）
    assert len(sources) == 13


def test_keyword_filters_admit_known_relevant_titles() -> None:
    """钉住几条「曾经被漏检、修好后必须继续命中」的真实标题。

    为什么需要这条测试
    ------------------
    关键词过滤是本项目最难察觉的一处覆盖缺口：它丢条目时不报错、
    流水线仍显示 `ok`，因此「关键词定窄了」这件事可以长期无人知晓。
    2026-10-05 补上丢弃计数后立刻发现两个真实漏检，各自靠加一个词修好。

    但「加一个词」是个脆弱状态——后来的人看到关键词表里有个孤零零的
    「网络」，很可能觉得与「人工智能」主题不搭而顺手删掉，
    这一删就是又一次静默漏检，且没有任何测试会红。

    因此这里把「哪条政策必须被哪个源放行」直接写死成断言。
    它不是重复实现过滤逻辑，而是一份**覆盖要求的清单**：
    只要这些标题仍应属于知识库，对应的关键词就不许删。

    维护方式：若某条政策经复核确认不属于本项目范围，
    应连同说明一起从下表删除，而不是只删关键词。
    """
    # 加载全部源
    _issuers, sources, _errors = load_sources()
    # 清单：(源 id, 必须被放行的标题, 为什么它属于本项目)
    required = [
        (
            "nfra-regulations",
            "国家金融监督管理总局就《银行业保险业网络安全管理办法（征求意见稿）》公开征求意见",
            "银行业网络安全管理办法，是 AI 系统在该行业落地必须遵守的配套规章",
        ),
        (
            "nfra-regulations",
            "中国人民银行 工业和信息化部市场监管总局 金融监管总局 中国证监会 国家知识产权局 国家网信办 国家外汇局有关负责人就《金融产品网络营销管理办法》答记者问",
            "本项目的金融×AI 交叉点条目，曾被长期漏检",
        ),
        (
            "nfra-regulations",
            "国家金融监督管理总局发布《银行业保险业数字金融高质量发展实施方案》",
            "数字金融行业级纲领，「数字化」不含「数据」，旧词表匹配不上",
        ),
        (
            "csrc-regulations",
            "【第218号令】《证券期货业网络和信息安全管理办法》",
            "证券期货业网络与信息安全配套规章，与银行业那部同构",
        ),
    ]
    # 逐条验证
    for source_id, title, reason in required:
        # 取该源的包含关键词与排除关键词
        filters = sources[source_id].filters or {}
        # 提取包含词
        include = [k for k in (filters.get("include_keywords") or []) if k]
        # 提取排除词
        exclude = [k for k in (filters.get("exclude_keywords") or []) if k]
        # 不应被任何排除词挡下
        assert not any(word in title for word in exclude), (
            f"[{source_id}] 标题被排除词挡下：{title}（{reason}）"
        )
        # 必须命中至少一个包含词
        assert any(word in title for word in include), (
            f"[{source_id}] 标题不含任何包含词，将被静默丢弃：{title}（{reason}）"
        )


def test_every_source_fetcher_is_registered() -> None:
    """验证每个数据源声明的抓取器类型都有对应实现。

    这条测试对应一个真实的坑：nfra 专用抓取器被删除后，
    若 ``data/sources.yaml`` 里仍写着 ``fetcher: nfra``，
    ``build_fetcher`` 会静默退回通用抓取器——表面上一切正常，
    实际上用的不是你以为的那个解析逻辑。这条测试把这种不一致变成失败。
    """
    # 加载登记表
    _issuers, sources, _errors = load_sources()
    # 逐个检查抓取器类型
    for source_id, source in sources.items():
        # 人工录入源不需要抓取器实现
        if source.fetcher == "manual":
            # 跳过
            continue
        # 断言该类型已注册
        assert source.fetcher in _FETCHER_REGISTRY, (
            f"数据源 {source_id} 声明了未注册的抓取器 {source.fetcher!r}；"
            f"已注册的类型为 {sorted(_FETCHER_REGISTRY)}"
        )


def test_enabled_sources_have_list_url_except_manual() -> None:
    """验证启用的网络抓取源都配置了列表页地址。"""
    # 加载登记表
    _issuers, sources, _errors = load_sources()
    # 逐个检查
    for source_id, source in sources.items():
        # 只检查启用且非人工录入的源
        if not source.enabled or source.fetcher == "manual":
            # 跳过
            continue
        # 断言有列表页地址
        assert source.list_url, f"启用的数据源 {source_id} 未配置 list_url"


def test_configured_api_blocks_are_well_formed() -> None:
    """验证配置了 api 节的源，其关键字段齐备。

    缺 items_path 或 field_map 会让抓取器「成功」返回 0 条，
    从而被误读为「没有新政策」——这是最需要避免的失败模式。
    """
    # 加载登记表
    _issuers, sources, _errors = load_sources()
    # 逐个检查
    for source_id, source in sources.items():
        # 只检查配置了 api 的源
        if not source.api:
            # 跳过
            continue
        # 必须配置接口路径
        assert source.api.get("path"), f"{source_id} 的 api 配置缺少 path"
        # 必须配置条目数组位置
        assert source.api.get("items_path"), f"{source_id} 的 api 配置缺少 items_path"
        # 必须配置字段映射
        assert source.api.get("field_map"), f"{source_id} 的 api 配置缺少 field_map"


def test_sources_file_exists() -> None:
    """验证数据源登记表文件路径存在。"""
    # 文件应真实存在
    assert SOURCES_FILE.exists()
    # 类型应为 Path（防止被误改成字符串常量）
    assert isinstance(SOURCES_FILE, Path)


# ============================================================
# 政策记录
# ============================================================

def test_all_policy_files_load_without_errors() -> None:
    """验证全部政策记录文件都能通过三层校验，且不含错误级问题。

    注意这里刻意用 ``split_issues`` 分流后再断言：``load_all_policies``
    返回的清单里同时包含警告（如「尚未经人工核验」），
    那些是显式记录的待办事项，不应导致测试失败。
    但错误级问题必须为零。
    """
    # 加载全部记录
    _policies, issues = load_all_policies()
    # 分流
    errors, _warnings = split_issues(issues)
    # 不应有任何错误级问题
    assert errors == [], f"存在错误级问题：{errors}"


def test_policy_to_dict_is_json_serializable_for_all_records() -> None:
    """验证每条记录的导出字典都能被 JSON 序列化。

    这条测试对应一个真实的崩溃：YAML 中 ``fetched_at`` 未加引号时会被
    PyYAML 解析成 ``datetime`` 对象，而字段声明为 ``str``。
    JSON Schema 校验不会发现这个问题（校验前已做日期归一化），
    直到有人执行 ``finreg show <id> --json`` 才崩在
    ``TypeError: Object of type datetime is not JSON serializable``。

    导出能力是知识库的基本要求（使用者要把数据接到自己的系统里），
    因此这里把「每个字段都能序列化」变成一条硬性断言。
    """
    # 加载全部记录
    policies, _issues = load_all_policies()
    # 逐条尝试序列化
    for policy_id, policy in policies.items():
        # 转字典并序列化；失败时 pytest 会抛出异常并指出是哪条记录
        try:
            # ensure_ascii=False 与 CLI 的输出方式保持一致
            json.dumps(policy_to_dict(policy), ensure_ascii=False)
        except TypeError as exc:
            # 转成带记录标识的断言失败信息，便于直接定位
            pytest.fail(f"记录 {policy_id} 无法序列化为 JSON：{exc}")


def test_policy_directory_is_not_empty() -> None:
    """验证政策目录中确实有记录——防止路径写错导致「校验通过但数据为空」。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 当前至少有 8 条种子记录
    assert len(policies) >= 8
    # 目录存在
    assert POLICIES_DIR.exists()


def test_cross_references_are_consistent() -> None:
    """验证版本链的双向指针闭合。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 校验跨记录引用
    problems = validate_cross_references(policies)
    # 不应有悬空引用
    assert problems == []


# ============================================================
# 整体校验入口（CI 门禁）
# ============================================================

def test_validate_repository_returns_three_lists() -> None:
    """验证校验入口返回 (错误, 警告, 陈旧记录) 三元组。"""
    # 执行校验
    errors, warnings, stale = validate_repository()
    # 三者都应为列表
    assert isinstance(errors, list)
    # 警告清单
    assert isinstance(warnings, list)
    # 陈旧清单
    assert isinstance(stale, list)


def test_validate_repository_has_no_errors() -> None:
    """验证仓库当前不存在错误级问题。

    警告是允许的（它们是显式记录的待办事项，如「尚未人工核验」），
    但错误必须为零——否则 CI 应当失败。
    """
    # 执行校验
    errors, _warnings, _stale = validate_repository(today=date(2026, 10, 3))
    # 断言无错误
    assert errors == [], f"仓库存在错误级问题：{errors}"


def test_validate_repository_warnings_are_all_prefixed_when_raw() -> None:
    """验证展示用的警告清单已剥离前缀，可读性良好。"""
    # 执行校验
    _errors, warnings, _stale = validate_repository(today=date(2026, 10, 3))
    # 剥离后的警告不应再含前缀
    for warning in warnings:
        # 逐条断言
        assert not warning.startswith("[警告]")


def test_split_issues_separates_errors_and_warnings() -> None:
    """验证问题分流逻辑正确。"""
    # 混合的问题列表
    issues = ["普通错误一", "[警告] 待办一", "普通错误二", "[警告] 待办二"]
    # 分流
    errors, warnings = split_issues(issues)
    # 错误应剥离出来
    assert errors == ["普通错误一", "普通错误二"]
    # 警告应剥离前缀
    assert warnings == ["待办一", "待办二"]


# ============================================================
# 陈旧记录检测
# ============================================================

def test_find_stale_policies_with_injected_reference_date() -> None:
    """验证陈旧检测能按注入的基准日期筛出超期未核验的记录。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 以一个极远的未来日期为基准，所有记录都应被判为陈旧
    far_future = date(2099, 1, 1)
    # 执行检测
    stale = find_stale_policies(policies, far_future, threshold_days=180)
    # 记录数应一致
    assert len(stale) == len(policies)


def test_find_stale_policies_returns_nothing_for_fresh_records() -> None:
    """验证基准日期在记录核验之前时，不产生陈旧记录。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 基准日期设为很早，所有记录的核验日都在其后
    long_ago = date(2000, 1, 1)
    # 执行检测
    stale = find_stale_policies(policies, long_ago, threshold_days=180)
    # 不应有陈旧记录
    assert stale == []


def test_find_stale_policies_returns_sorted_tuples() -> None:
    """验证返回结构为 (政策 id, 天数) 且按天数降序排列。"""
    # 加载全部记录
    policies, _errors = load_all_policies()
    # 以远未来为基准
    stale = find_stale_policies(policies, date(2099, 1, 1), threshold_days=180)
    # 天数应为降序
    days = [item[1] for item in stale]
    # 断言排序
    assert days == sorted(days, reverse=True)
    # 每项应为二元组
    assert all(len(item) == 2 for item in stale)


# ============================================================
# 内容哈希与文本归一化
# ============================================================

def test_compute_hash_is_deterministic() -> None:
    """验证相同输入产生相同哈希。"""
    # 两次计算结果应一致
    assert compute_hash("相同内容") == compute_hash("相同内容")


def test_compute_hash_differs_for_different_inputs() -> None:
    """验证不同输入产生不同哈希。"""
    # 不同内容应产生不同哈希
    assert compute_hash("内容甲") != compute_hash("内容乙")


def test_compute_hash_uses_sha256_prefix() -> None:
    """验证哈希带算法前缀，便于将来平滑更换算法。"""
    # 断言前缀
    assert compute_hash("abc").startswith("sha256:")


def test_normalize_text_collapses_layout_differences() -> None:
    """验证归一化能消除排版差异——这是避免「改个访问计数就误报变更」的关键。"""
    # 两份「实质相同、排版不同」的文本
    a = "标题\n\n\n正文  第一行\n正文第二行"
    # 行首尾空白与空行数量不同
    b = "  标题  \n正文  第一行\n\n正文第二行   "
    # 归一化后应完全相同
    assert normalize_text(a) == normalize_text(b)


def test_normalize_text_keeps_substantive_changes() -> None:
    """验证归一化不会抹掉实质差异。"""
    # 实质不同的两段文本
    a = "第一条 应当建立风险管理制度"
    # 关键词不同
    b = "第一条 应当建立数据管理制度"
    # 归一化后仍应不同
    assert normalize_text(a) != normalize_text(b)


# ============================================================
# 固定装置自身的完整性
# ============================================================

@pytest.mark.parametrize(
    "fixture_name",
    ["nfra_doc_list.json", "csrc_search_list.json", "pbc_common_table.html"],
)
def test_fixtures_exist(fixtures_dir: Path, fixture_name: str) -> None:
    """验证测试固定装置文件存在。

    固定装置若被误删，相关测试会以「解析出 0 条」的形式失败，
    报错信息看起来像抓取器坏了。这条测试让原因直接可见。
    """
    # 断言文件存在
    assert (fixtures_dir / fixture_name).exists(), f"固定装置缺失：{fixture_name}"


# ============================================================
# 执法记录（罚单）的加载与引用闭合
# ------------------------------------------------------------
# data/enforcement/ 目前是空目录，但这一层必须现在就测：
# 它是「抓取 → 人判断 → 录入 → 关联 → 校验」这条闭环的后半段，
# 等第一批记录进来才发现加载器有问题，代价会大得多。
# 这里用 tmp_path 造目录，全程不碰仓库里的真实数据。
# ============================================================

def make_enforcement_data(record_id: str = "pbc-2026-yinfa-104", **overrides: object) -> dict:
    """构造一份可直接写入 YAML 的执法记录字典。"""
    # 默认值取「一切正常」的形态
    data: dict = {
        "id": record_id,                                   # 标识
        "decision_no": "银罚决字〔2026〕104号",             # 文号
        "party": "某某银行股份有限公司",                     # 当事人
        "party_type": "institution",                       # 当事人类型
        "violation_type": "违反数据安全管理规定",            # 违规事实
        "penalty_content": "警告，罚款100万元",             # 处罚内容
        "authority": "中国人民银行",                        # 决定机关
        "legal_basis": None,                               # 处罚依据（官方不提供）
        "decision_date": "2026年9月8日",                    # 决定日期
        "published_on": "2026-09-24",                      # 公示日期
        "publicity_period": "五年",                         # 公示期限
        "jurisdiction": "CN",                              # 法域
        "domain": ["数据安全"],                             # 涉及领域
        "related_policies": [],                            # 关联政策
        "source": {                                        # 溯源
            "url": "https://www.pbc.gov.cn/x/index.html",
            "site": "pbc.gov.cn",
            "fetched_at": "2026-10-05T22:31:00+08:00",
            "tier": "primary",
            "http_status": 200,
        },
        "last_verified": None,                             # 尚未人工核验
        "verified_by": "automated",                        # 核验方式
        "notes": "字段抄自官方公示原文，未逐项人工核对",      # 备注
    }
    # 应用覆盖项
    data.update(overrides)
    # 返回
    return data


def write_enforcement_yaml(directory: Path, data: dict) -> Path:
    """把字典写成 data/enforcement 下的 YAML 文件，返回路径。

    刻意走生产代码的写出路径（``enforcement_from_dict`` → ``save_enforcement``），
    而不是在测试里自己 ``safe_dump``：这样写入端与读取端用的是同一套字段映射，
    一旦某一边漏了字段，往返就会暴露；测试自己造文件则会把这类缺陷掩盖掉。
    """
    # 先转成模型对象，再交给生产写出函数
    return save_enforcement(enforcement_from_dict(data), directory)


def test_enforcement_directory_is_trackable() -> None:
    """验证 data/enforcement/ 目录存在且带有说明文件。

    目录为空时 git 不会跟踪它（空目录不是版本控制的对象），
    因此必须有一个文件把目录留住。缺了它，CI 检出后的仓库里
    没有这个目录，任何「往这里写记录」的流程都会因为目录不存在而出错。
    """
    # 目录必须存在
    assert ENFORCEMENT_DIR.is_dir(), f"执法记录目录不存在：{ENFORCEMENT_DIR}"
    # 且必须有说明文件（README 同时承担「把空目录留住」这个职责）
    assert (ENFORCEMENT_DIR / "README.md").is_file(), "data/enforcement/ 缺少 README.md"


def test_load_all_enforcement_returns_empty_for_missing_directory(tmp_path: Path) -> None:
    """验证目录不存在时返回空结果而不报错。

    这是与 load_all_policies 有意为之的差别：「目前还没有核验过的罚单」
    是一个合法状态。若把空目录当错误，维护者会被逼着往里塞一条凑数的
    记录来让 CI 变绿——那比没有记录更糟，因为它制造了不可信的条目。
    """
    # 指向一个并不存在的目录
    records, errors = load_all_enforcement(tmp_path / "not-exists")
    # 空结果
    assert records == {}
    # 且没有任何错误
    assert errors == []


def test_load_enforcement_file_accepts_valid_record(tmp_path: Path) -> None:
    """验证一条形态正确的记录能被加载，且逐字字段一字不差。"""
    # 写入文件
    path = write_enforcement_yaml(tmp_path, make_enforcement_data())
    # 加载
    record, errors = load_enforcement_file(path)
    # 无错误
    assert errors == []
    # 记录已构造
    assert record is not None
    # 文号含中文括号，逐字保留
    assert record.decision_no == "银罚决字〔2026〕104号"
    # 决定日期保留中文写法
    assert record.decision_date == "2026年9月8日"
    # 公示日期被解析为 date
    assert record.published_on == date(2026, 9, 24)


def test_load_enforcement_file_reports_schema_violation(tmp_path: Path) -> None:
    """验证结构错误被 schema 拦下并报到具体字段。"""
    # id 不符合 schema 的格式要求
    path = write_enforcement_yaml(tmp_path, make_enforcement_data(record_id="不合规的标识"))
    # 加载
    record, errors = load_enforcement_file(path)
    # 记录未构造
    assert record is None
    # 有错误
    assert errors
    # 错误信息带文件名，便于定位
    assert all(path.name in err for err in errors)


def test_load_enforcement_file_reports_semantic_warning(tmp_path: Path) -> None:
    """验证语义问题被报出，且仍是警告级（不会阻断）。"""
    # 违规含技术类词但未归类，应产生归类提示
    path = write_enforcement_yaml(tmp_path, make_enforcement_data(domain=[]))
    # 加载
    record, errors = load_enforcement_file(path)
    # 记录仍然构造出来（警告不阻断）
    assert record is not None
    # 有一条警告
    assert len(errors) == 1
    # 是警告级
    assert errors[0].startswith("[警告]")
    # 且带上了文件名（with_location 必须把警告标记保持在最前，否则会被判为错误）
    assert path.name in errors[0]


def test_load_all_enforcement_detects_duplicate_ids(tmp_path: Path) -> None:
    """验证重复 id 被拦截——重复会让 related_policies 的引用产生歧义。"""
    # 同一 id 写两次，文件名不同
    write_enforcement_yaml(tmp_path, make_enforcement_data())
    # 手工再造一个同 id 的文件，文件名刻意取别的名字，
    # 以覆盖「文件名不同但 id 相同」这种最容易漏掉的形态。
    # 这里不能用 save_enforcement —— 它按 id 推导文件名，写出来会覆盖前一个，
    # 而本测试要验证的恰恰是「文件名与 id 不一致时，重复 id 仍要被发现」。
    duplicate_path = tmp_path / "another-file.yaml"
    # 写入
    with open(duplicate_path, "w", encoding="utf-8", newline="\n") as fh:
        # 保留键顺序
        yaml.safe_dump(make_enforcement_data(), fh, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    # 加载整个目录
    records, errors = load_all_enforcement(tmp_path)
    # 只登记了一条
    assert len(records) == 1
    # 且报了重复
    assert any("id 重复" in err for err in errors)


def test_validate_enforcement_references_flags_dangling_policy_id(tmp_path: Path) -> None:
    """反向测试：罚单引用了一条不存在的政策时必须报错。

    这是本类记录里最容易被忽视的失效方式：related_policies 是本目录
    唯一把「监管实际处罚的行为」连到「我们提炼出的义务」的字段，
    但它同时最容易悄悄失效——政策记录被改名或下架时，
    罚单这一侧的引用不会自动跟着改，于是链接静默断裂。
    断链的罚单表面上和正常记录长得一模一样，只有点进去才会发现，
    而没有人会逐条点进去。
    """
    # 一条指向不存在政策的罚单
    write_enforcement_yaml(tmp_path, make_enforcement_data(related_policies=["csrc-2026-not-exist"]))
    # 加载
    records, errors = load_all_enforcement(tmp_path)
    # 加载本身没问题（引用闭合是另一层校验）
    assert errors == []
    # 引用校验必须报错
    problems = validate_enforcement_references(records, {})
    # 有一条问题
    assert len(problems) == 1
    # 明确指出断链的目标 id
    assert "csrc-2026-not-exist" in problems[0]
    # 且说明后果，便于修复者判断严重性
    assert "related_policies" in problems[0]


def test_validate_enforcement_references_accepts_existing_policy_id(tmp_path: Path) -> None:
    """验证引用存在的政策时不报错（反向对照）。"""
    # 真实存在的政策
    policies, _errors = load_all_policies(POLICIES_DIR)
    # 取一个真实 id
    existing_id = next(iter(policies))
    # 构造引用它的罚单
    write_enforcement_yaml(tmp_path, make_enforcement_data(related_policies=[existing_id]))
    # 加载
    records, _load_errors = load_all_enforcement(tmp_path)
    # 引用闭合
    assert validate_enforcement_references(records, policies) == []


def test_validate_repository_reports_dangling_enforcement_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向测试：finreg validate 必须真的检查执法记录这一维度。

    做法：把执法记录目录指向一个临时目录，里面放一条引用不存在政策的罚单，
    再跑一次完整校验，断言结果是**不通过**。

    为什么不能只用真实的 data/enforcement/（它当前是空的）：
    「空目录通过校验」与「这一维度根本没接上」在结果上完全一样，
    永远无法区分。必须注入一份有问题的数据，才能证明闸门确实接上了。
    """
    # 造一条断链罚单
    write_enforcement_yaml(tmp_path, make_enforcement_data(related_policies=["pbc-1999-ghost"]))
    # 把执法记录目录指到临时目录。
    # 这里改的是 store 模块的属性而不是某个局部变量——
    # load_all_enforcement 在调用时从模块全局读该常量，因此替换即时生效。
    monkeypatch.setattr("finreg_ai.store.ENFORCEMENT_DIR", tmp_path)
    # 跑一次完整校验
    errors, _warnings, _stale = validate_repository()
    # 必须报错
    assert errors, "执法记录的断链没有被 validate 拦下——这一维度可能没接上"
    # 且错误里能看到那条断链
    assert any("pbc-1999-ghost" in err for err in errors)
