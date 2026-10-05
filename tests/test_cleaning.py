"""条目清洗层单元测试 —— 去重、导航链接识别、关键词过滤。

为什么这一层是测试重点
----------------------
本项目把「静默污染」视为头号敌人：抓取器最危险的失败方式不是崩溃，
而是「成功地」抓回一堆导航链接、统计数据和页脚文字，同时报告 status=ok。
这一层就是拦截它的地方，因此每个判定条件都要有正面与反面的测试。
"""

# 导入 date 类型
from datetime import date

# 导入 pytest 以使用参数化
import pytest

# 导入待测函数
from finreg_ai.fetchers.base import (
    DROP_REASON_EXCLUDED,
    DROP_REASON_NOT_MATCHED,
    NAV_TITLE_MAX_LEN,
    RawDoc,
    _url_path_depth,
    apply_filters,
    dedupe_by_url,
    drop_probable_navigation,
    filter_docs_with_reasons,
    filter_haystack,
)

# 人民银行规范性文件列表页的真实地址，作为「列表页层数」的比较基准
PBC_LIST_URL = "http://www.pbc.gov.cn/tiaofasi/144941/3581332/index.html"


def make_doc(
    title: str,
    url: str,
    published_on: date | None = None,
    extra: dict | None = None,
) -> RawDoc:
    """构造测试用 ``RawDoc``，减少重复样板代码。

    ``extra`` 默认为 None 而不是 {}：``RawDoc.extra`` 的默认值就是空字典，
    若这里默认给一个可变字面量，多个测试之间会共享同一个 dict 对象，
    一处改动会影响别处——这类串味极难排查。
    """
    # 组装条目，source_id 固定为测试标识
    return RawDoc(
        title=title,
        url=url,
        source_id="test",
        published_on=published_on,
        extra=extra or {},
    )


# ============================================================
# 链接层数计算
# ============================================================

@pytest.mark.parametrize(
    ("url", "expected_depth"),
    [
        # 列表页：tiaofasi / 144941 / 3581332 / index.html
        (PBC_LIST_URL, 4),
        # 栏目入口：kejisi / 146812 / index.html
        ("http://www.pbc.gov.cn/kejisi/146812/index.html", 3),
        # 政策详情页：比列表页多一层文档标识
        ("http://www.pbc.gov.cn/tiaofasi/144941/3581332/2026091114591687697/index.html", 5),
        # 带查询串时查询串不计入层数（csrc / c101953 / common_list.shtml）
        ("https://www.csrc.gov.cn/csrc/c101953/common_list.shtml?channelid=abc", 3),
        # 带锚点同理（x / y / index.html）
        ("https://a.cn/x/y/index.html#top", 3),
        # 根路径
        ("https://a.cn/", 0),
        # 空串
        ("", 0),
    ],
)
def test_url_path_depth(url: str, expected_depth: int) -> None:
    """验证路径层数计算对各种写法的处理。"""
    # 断言层数
    assert _url_path_depth(url) == expected_depth


# ============================================================
# 去重
# ============================================================

def test_dedupe_by_url_removes_duplicates_keeping_first() -> None:
    """验证按链接去重，并保留首次出现的顺序。

    这个测试对应一个真实的 bug：PbcFetcher 曾因未复用去重逻辑，
    让同一条公告在结果里出现三次。
    """
    # 三个条目中有两个链接相同
    docs = [
        make_doc("文件甲", "http://a.cn/1.html", date(2026, 1, 1)),
        make_doc("文件乙", "http://a.cn/2.html", date(2026, 1, 2)),
        make_doc("文件甲（重复）", "http://a.cn/1.html", date(2026, 1, 1)),
    ]
    # 执行去重
    result = dedupe_by_url(docs)
    # 应剩下两条
    assert len(result) == 2
    # 首次出现的顺序优先
    assert [d.title for d in result] == ["文件甲", "文件乙"]


def test_dedupe_by_url_keeps_same_title_different_url() -> None:
    """验证同名但不同链接的文件不会被误删。

    政府网站常有同名文件（如多个年份的「通知」），
    因此去重必须按链接而非标题——按标题去重会丢真实数据。
    """
    # 两条标题相同但链接不同
    docs = [
        make_doc("关于开展某工作的通知", "http://a.cn/2025.html", date(2025, 1, 1)),
        make_doc("关于开展某工作的通知", "http://a.cn/2026.html", date(2026, 1, 1)),
    ]
    # 两条都应保留
    assert len(dedupe_by_url(docs)) == 2


def test_dedupe_by_url_handles_empty_input() -> None:
    """验证空输入返回空列表。"""
    # 空列表
    assert dedupe_by_url([]) == []


# ============================================================
# 栏目导航链接识别 —— 本项目最重要的清洗规则
# ============================================================

def test_drop_probable_navigation_removes_column_links() -> None:
    """验证栏目导航链接被剔除。

    这条测试直接对应一个真实事故：人行列表页的「金融科技」栏目入口
    因标题含关键词「科技」，在关键词过滤后成了唯一命中项，
    导致流水线在抓到 0 条真实政策的情况下报告 ok。
    """
    # 导航链接：无日期、链接比列表页浅、标题短
    docs = [
        make_doc("金融科技", "http://www.pbc.gov.cn/kejisi/146812/index.html"),
        make_doc("新闻发布", "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"),
        make_doc("法律声明", PBC_LIST_URL),
    ]
    # 全部应被剔除
    assert drop_probable_navigation(docs, PBC_LIST_URL) == []


def test_drop_probable_navigation_keeps_real_policy_docs() -> None:
    """验证真实政策条目被保留。

    政策详情页比列表页深一层，且带发布日期，两个条件都不满足剔除规则。
    """
    # 真实政策条目
    docs = [
        make_doc(
            "中国人民银行公告〔2026〕第24号",
            "http://www.pbc.gov.cn/tiaofasi/144941/3581332/2026091114591687697/index.html",
            date(2026, 9, 11),
        ),
    ]
    # 应保留
    assert len(drop_probable_navigation(docs, PBC_LIST_URL)) == 1


def test_drop_probable_navigation_keeps_undated_doc_when_url_is_deeper() -> None:
    """验证「无日期但链接更深」的条目被保留。

    这是刻意保守的设计：三个条件必须同时满足才剔除。
    某些站点的列表页不显示日期，此时若仅凭「无日期」就剔除，
    会把真实条目全部误删——那是不可逆的损失。
    """
    # 链接比列表页深，但没有日期
    docs = [
        make_doc(
            "某份没有显示发布日期的政策文件",
            "http://www.pbc.gov.cn/tiaofasi/144941/3581332/2026010100000000001/index.html",
        ),
    ]
    # 应保留（条件 1 满足但条件 2 不满足）
    assert len(drop_probable_navigation(docs, PBC_LIST_URL)) == 1


def test_drop_probable_navigation_keeps_undated_doc_with_long_title() -> None:
    """验证「无日期、链接浅、但标题长」的条目被保留。

    长标题是「这是真实文件而非栏目入口」的强信号。
    """
    # 标题长于阈值
    long_title = "关" * (NAV_TITLE_MAX_LEN + 1)
    # 构造条目：链接浅、无日期，但标题足够长
    docs = [make_doc(long_title, "http://www.pbc.gov.cn/kejisi/146812/index.html")]
    # 应保留
    assert len(drop_probable_navigation(docs, PBC_LIST_URL)) == 1


def test_drop_probable_navigation_keeps_dated_doc_even_if_shallow() -> None:
    """验证「有日期」时无条件保留——日期是最强的否定信号。"""
    # 链接浅、标题短，但有日期
    docs = [make_doc("金融科技", "http://www.pbc.gov.cn/kejisi/146812/index.html", date(2026, 1, 1))]
    # 应保留
    assert len(drop_probable_navigation(docs, PBC_LIST_URL)) == 1


def test_drop_probable_navigation_handles_empty_list_url() -> None:
    """验证列表页地址为空时不会误删条目。

    列表页地址缺失时层数基准为 0，任何条目都「更深」，
    因此应当全部保留，而不是判为导航链接。
    """
    # 两个普通条目
    docs = [make_doc("某文件", "http://a.cn/x/y.html"), make_doc("另一文件", "http://a.cn/z.html")]
    # 应全部保留
    assert len(drop_probable_navigation(docs, "")) == 2


# ============================================================
# 关键词过滤
# ============================================================

def test_apply_filters_keeps_matching_titles() -> None:
    """验证命中包含关键词的条目被保留。"""
    # 条目
    docs = [
        make_doc("关于加强人工智能应用管理的通知", "http://a.cn/1.html", date(2026, 1, 1)),
        make_doc("关于开展某体育活动的通知", "http://a.cn/2.html", date(2026, 1, 2)),
    ]
    # 过滤规则只保留含「人工智能」的
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": []}
    # 执行过滤
    result = apply_filters(docs, filters)
    # 只剩一条
    assert len(result) == 1
    # 且是正确的那条
    assert result[0].title == "关于加强人工智能应用管理的通知"


def test_apply_filters_exclude_takes_precedence_over_include() -> None:
    """验证排除规则的优先级高于包含规则。

    典型场景：「行政处罚决定书」可能含「人工智能」字样，
    但它是个案处罚，不属于政策文件，必须排除。
    """
    # 该条同时命中包含词与排除词
    docs = [make_doc("关于某人工智能公司行政处罚的决定", "http://a.cn/1.html", date(2026, 1, 1))]
    # 规则：包含「人工智能」，排除「行政处罚」
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": ["行政处罚"]}
    # 排除优先，因此结果为空
    assert apply_filters(docs, filters) == []


def test_apply_filters_keeps_all_when_no_include_configured() -> None:
    """验证未配置包含规则时全部保留（只应用排除规则）。"""
    # 三条普通条目
    docs = [
        make_doc("文件甲", "http://a.cn/1.html", date(2026, 1, 1)),
        make_doc("文件乙", "http://a.cn/2.html", date(2026, 1, 2)),
        make_doc("文件丙", "http://a.cn/3.html", date(2026, 1, 3)),
    ]
    # 只有排除规则且不含「乙」
    filters = {"exclude_keywords": ["乙"]}
    # 应保留甲丙
    titles = [d.title for d in apply_filters(docs, filters)]
    assert titles == ["文件甲", "文件丙"]


def test_apply_filters_returns_all_when_filters_is_none() -> None:
    """验证过滤规则为 None 时原样返回全部条目。"""
    # 两条条目
    docs = [make_doc("文件甲", "http://a.cn/1.html"), make_doc("文件乙", "http://a.cn/2.html")]
    # 无过滤配置
    assert len(apply_filters(docs, None)) == 2


# ============================================================
# 关键词过滤 —— 丢弃计数
# ------------------------------------------------------------
# 为什么这一组测试比上面那组更重要
# --------------------------------
# apply_filters 只回答「留下了什么」，不回答「丢掉了什么」。而本项目的
# 头号敌人是静默丢弃：一份标题不含「人工智能」、正文却含 AI 条款的文件
# 会被关键词规则挡掉，抓取报告依旧显示 ok，没有任何地方会提示有人被丢了。
#
# filter_docs_with_reasons 的存在就是为了让这种丢弃可见。因此下面除了
# 「计数是否正确」，还专门写了一条反向测试（第 6 条），断言「看起来不相关
# 的文件被丢弃时会被计数」——若哪天有人把计数逻辑优化掉了，那条测试会红。
# ============================================================

def test_filter_docs_with_reasons_counts_not_matched() -> None:
    """验证「未命中包含词」的条目被单独计数。"""
    # 一条命中包含词、两条不命中
    docs = [
        make_doc("关于加强人工智能应用管理的通知", "http://a.cn/1.html"),
        make_doc("关于开展全民健身活动的通知", "http://a.cn/2.html"),
        make_doc("关于调整办公用房面积标准的通知", "http://a.cn/3.html"),
    ]
    # 规则只保留含「人工智能」的
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": []}
    # 执行过滤，拿保留结果与丢弃原因
    kept, reasons = filter_docs_with_reasons(docs, filters)
    # 只保留一条
    assert len(kept) == 1
    # 另两条计入「未命中包含词」
    assert reasons == {DROP_REASON_NOT_MATCHED: 2}


def test_filter_docs_with_reasons_counts_excluded() -> None:
    """验证「命中排除词」的条目被单独计数。

    与 not_matched 分开计数的价值：excluded 多说明排除词写得过宽，
    not_matched 多说明包含词写得过窄——两者要采取的行动完全相反，
    混在一起就失去了诊断能力。
    """
    # 一条命中排除词、一条命中包含词
    docs = [
        make_doc("关于某人工智能公司行政处罚的决定", "http://a.cn/1.html"),
        make_doc("关于加强人工智能应用管理的通知", "http://a.cn/2.html"),
    ]
    # 规则：包含「人工智能」，排除「行政处罚」
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": ["行政处罚"]}
    # 执行过滤
    kept, reasons = filter_docs_with_reasons(docs, filters)
    # 保留的是没被排除的那条
    assert [d.title for d in kept] == ["关于加强人工智能应用管理的通知"]
    # 被排除的单独计数
    assert reasons == {DROP_REASON_EXCLUDED: 1}


def test_filter_docs_with_reasons_reasons_sum_equals_dropped_total() -> None:
    """验证「各原因之和 == 丢弃总数」的守恒关系。

    这条不变式是上层统计的正确性基础：FetchResult.dropped_by_filter 就是
    各原因之和，若两者能对不上，报告的丢弃数就是错的。
    """
    # 构造五条：一条保留、两条被排除、两条未命中
    docs = [
        make_doc("人工智能应用管理办法", "http://a.cn/1.html"),          # 保留
        make_doc("人工智能公司行政处罚决定", "http://a.cn/2.html"),      # 被排除
        make_doc("人工智能企业行政许可批复", "http://a.cn/3.html"),      # 被排除
        make_doc("全民健身活动通知", "http://a.cn/4.html"),              # 未命中
        make_doc("办公用房面积标准", "http://a.cn/5.html"),              # 未命中
    ]
    # 规则
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": ["行政处罚", "行政许可"]}
    # 执行过滤
    kept, reasons = filter_docs_with_reasons(docs, filters)
    # 明细之和必须等于「输入减去保留」
    assert sum(reasons.values()) == len(docs) - len(kept)
    # 且明细本身符合预期
    assert reasons == {DROP_REASON_EXCLUDED: 2, DROP_REASON_NOT_MATCHED: 2}


def test_filter_docs_with_reasons_returns_empty_reasons_without_config() -> None:
    """验证未配置过滤规则时不产生任何丢弃原因。

    这条防止一种误报：没有任何过滤规则时却报告「丢弃了 N 条」，
    会让人去追一个不存在的配置问题。
    """
    # 三条条目
    docs = [make_doc(f"文件{i}", f"http://a.cn/{i}.html") for i in range(1, 4)]
    # 无过滤配置
    kept, reasons = filter_docs_with_reasons(docs, None)
    # 全部保留
    assert len(kept) == 3
    # 且原因是空字典（不是 {"not_matched": 0} 这类含零值的形式）
    assert reasons == {}


def test_filter_docs_with_reasons_ignores_blank_keywords() -> None:
    """验证关键词列表中的空串被忽略，不会让整源全量通过。

    空串会让 ``"" in title`` 恒为真，于是「包含规则」形同不存在——
    这是最容易被忽略的一种配置失误：YAML 里多写一个 ``-`` 或写成
    ``- ""`` 就会触发，而且表现为「今天这条源数据特别全」，
    比报错更难发现。
    """
    # 一条与关键词毫无关系的条目
    docs = [make_doc("关于调整办公用房面积标准的通知", "http://a.cn/1.html")]
    # 包含词里混入空串与 None，只有「人工智能」是真词
    filters = {"include_keywords": ["", None, "人工智能"], "exclude_keywords": []}
    # 执行过滤。这里刻意混入 None 与空串——YAML 里写成 `- ` 就是这个效果
    kept, reasons = filter_docs_with_reasons(docs, filters)
    # 空串没有让该条被保留
    assert kept == []
    # 而是照常计入了未命中
    assert reasons == {DROP_REASON_NOT_MATCHED: 1}


def test_filter_docs_with_reasons_makes_silent_drop_visible() -> None:
    """反向测试：标题不含 AI 字样但可能含 AI 条款的文件被丢弃时会被计数。

    这是本功能存在的唯一理由，因此必须有一条测试直接钉住它。
    构造一份《关于数据治理的通知》——它命中包含词「数据」，会被保留；
    再构造一份《关于加强信息科技外包管理的通知（正文含 AI 条款）》，
    标题里没有当前包含词中的任何一个，会被丢弃。
    断言的重点不是「它被丢了」，而是**它被丢了并且被计数**。
    """
    # 旧规则：只有这几个词（模拟本次扩容前的 nfra-regulations）
    legacy_filters = {"include_keywords": ["人工智能", "智能", "算法", "模型", "数据", "科技"]}
    # 一份标题没有 AI 字样、但属于数字金融治理的文件
    docs = [
        make_doc("国家金融监督管理总局发布《银行业保险业数字金融高质量发展实施方案》", "http://a.cn/1.html"),
        make_doc("国家金融监督管理总局就《银行业保险业网络安全管理办法（征求意见稿）》公开征求意见", "http://a.cn/2.html"),
    ]
    # 用旧规则过滤
    kept, reasons = filter_docs_with_reasons(docs, legacy_filters)
    # 旧规则下两条都被丢弃
    assert kept == []
    # 关键断言：丢弃被记录下来了，而不是无影无踪
    assert reasons == {DROP_REASON_NOT_MATCHED: 2}
    # 换成扩容后的规则（新增「网络」「数字」），两条应被放行
    widened_filters = {
        "include_keywords": ["人工智能", "智能", "算法", "模型", "数据", "科技", "网络", "数字"]
    }
    # 重新过滤
    kept2, reasons2 = filter_docs_with_reasons(docs, widened_filters)
    # 两条都留下
    assert len(kept2) == 2
    # 且没有丢弃
    assert reasons2 == {}


def test_apply_filters_agrees_with_filter_docs_with_reasons() -> None:
    """验证薄封装与本体不会各自演化出不同结果。

    apply_filters 现在只是转调本体。保留它是为了不破坏既有调用方，
    但两份行为一旦分叉，就会出现「测试覆盖的是旧实现、线上跑的是新实现」
    这类最难排查的问题。这条测试把两者绑死。
    """
    # 混合场景：保留、被排除、未命中各若干
    docs = [
        make_doc("人工智能应用管理办法", "http://a.cn/1.html"),
        make_doc("人工智能公司行政处罚决定", "http://a.cn/2.html"),
        make_doc("全民健身活动通知", "http://a.cn/3.html"),
    ]
    # 规则
    filters = {"include_keywords": ["人工智能"], "exclude_keywords": ["行政处罚"]}
    # 两种调用方式
    via_wrapper = apply_filters(docs, filters)
    # 本体调用
    via_direct, _reasons = filter_docs_with_reasons(docs, filters)
    # 结果必须完全一致
    assert [d.url for d in via_wrapper] == [d.url for d in via_direct]


# ============================================================
# 关键词匹配范围：标题 + extra 里的结构化字段
# ------------------------------------------------------------
# 这一段的存在理由是一次实测到的静默丢失：罚单抓取器把「违法行为类型」
# 截断后拼进标题，某个关键词落在截断点之后——甚至被切在词中间——
# 就从过滤视野里消失了。记录被丢弃，原因记为「未命中包含词」，
# 与「这条罚单确实与数据安全无关」在报告里长得一模一样。
# ============================================================

def test_filter_haystack_concatenates_title_and_extra_strings() -> None:
    """验证匹配范围包含标题与 extra 里的全部字符串值。"""
    # 构造一条带结构化字段的记录
    doc = make_doc("广发银行股份有限公司｜1.违反金融统计管理规定…", "http://a.cn/1.html", extra={
        "violation_type": "1.违反金融统计管理规定; 5.违反数据安全管理规定",
        "authority": "中国人民银行",
        "document_url": "http://a.cn/doc.html",
    })
    # 取匹配文本
    haystack = filter_haystack(doc)
    # 标题在
    assert "广发银行股份有限公司" in haystack
    # extra 里的违规事实在
    assert "违反数据安全管理规定" in haystack
    # extra 里的机关名也在（本项目不做字段白名单——那是配置该管的事）
    assert "中国人民银行" in haystack


def test_filter_haystack_ignores_non_string_extra_values() -> None:
    """验证数字等非字符串值不参与匹配。

    结构化字段里可能有计数、金额这类数字。让它们参与中文关键词比较
    没有意义，而 ``"20" in 2026`` 这类比较在类型层面就会直接报错。
    """
    # 构造含数字与 None 的记录
    doc = make_doc("某通知", "http://a.cn/1.html", extra={"page": 3, "amount": None, "empty": ""})
    # 取匹配文本
    haystack = filter_haystack(doc)
    # 只有标题这一行
    assert haystack.strip() == "某通知"


def test_filter_haystack_inserts_separator_between_fields() -> None:
    """反向测试：不能把相邻字段首尾拼出一个原本不存在的词。

    若用空串连接，``title="违反"`` + ``extra={"x": "数据安全规定"}``
    会拼出「违反数据安全规定」——而这条记录里其实没有这个词。
    换行符把两个字段隔开，中文关键词不含换行，因此跨字段的假命中不会成立。

    这条测试的价值在于：它是一个**看起来无害的优化**（「直接 join 起来就行」）
    被挡住的地方。
    """
    # 标题以「违反」结尾，extra 以「数据安全」开头
    doc = make_doc("某银行违反", "http://a.cn/1.html", extra={"violation_type": "数据安全规定"})
    # 取匹配文本
    haystack = filter_haystack(doc)
    # 各字段自身仍然可见
    assert "违反" in haystack
    assert "数据安全" in haystack
    # 但跨字段拼出的词不存在
    assert "违反数据安全" not in haystack


def test_filter_keeps_record_matched_only_by_extra_field() -> None:
    """验证「只有 extra 里的字段含关键词」的记录能通过过滤。

    这是罚单场景的真实形态：标题被截断，「数据安全」只留在 extra 里。
    若这条断言失败，说明过滤又退回了「只看标题」，
    罚单数据会在检索层面整体消失，而报告显示 ok。
    """
    # 一条标题完全不含关键词、只有 extra 含关键词的记录
    doc = make_doc("广发银行股份有限公司｜1.违反金融统计管理规定…", "http://a.cn/1.html", extra={
        "violation_type": "5.违反数据安全管理规定",
    })
    # 只配「数据安全」这一个包含词
    filters = {"include_keywords": ["数据安全"], "exclude_keywords": []}
    # 过滤
    kept, reasons = filter_docs_with_reasons([doc], filters)
    # 必须保留
    assert [d.url for d in kept] == ["http://a.cn/1.html"]
    # 且不产生任何丢弃计数
    assert reasons == {}


def test_filter_excludes_record_matched_only_by_extra_field() -> None:
    """验证排除规则同样作用于 extra 字段（两条规则的范围必须一致）。

    若包含规则看 extra 而排除规则只看标题，就会出现一个隐蔽的不对称：
    一份含排除词的文件仅仅因为标题里没写而侥幸留下。
    不对称的规则比两边都窄更危险——它无法用一句话解释。
    """
    # 标题干净、extra 里含排除词
    doc = make_doc("某银行行政处罚公示", "http://a.cn/1.html", extra={"violation_type": "违反反假货币业务管理规定"})
    # 排除词只出现在 extra 里
    filters = {"include_keywords": ["银行"], "exclude_keywords": ["反假货币"]}
    # 过滤
    kept, reasons = filter_docs_with_reasons([doc], filters)
    # 应被排除
    assert kept == []
    # 原因记为「命中排除词」而不是「未命中包含词」
    assert reasons == {DROP_REASON_EXCLUDED: 1}


def test_filter_drops_extra_matched_doc_when_no_include_configured() -> None:
    """验证「无包含词时全保留」这条规则没有被 extra 匹配破坏。

    包含词为空表示「不做筛选」。此时即使某条记录的 extra 里有关键词，
    也应原样保留——否则会引入一条「extra 里有东西就留下」的隐性规则。
    """
    # 一条 extra 内容丰富的记录
    doc = make_doc("某通知", "http://a.cn/1.html", extra={"violation_type": "违反数据安全管理规定"})
    # 只配排除词、不配包含词
    filters = {"include_keywords": [], "exclude_keywords": ["不存在"]}
    # 过滤
    kept, reasons = filter_docs_with_reasons([doc], filters)
    # 保留
    assert [d.url for d in kept] == ["http://a.cn/1.html"]
    # 无丢弃
    assert reasons == {}
