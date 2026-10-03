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
    NAV_TITLE_MAX_LEN,
    RawDoc,
    _url_path_depth,
    apply_filters,
    dedupe_by_url,
    drop_probable_navigation,
)

# 人民银行规范性文件列表页的真实地址，作为「列表页层数」的比较基准
PBC_LIST_URL = "http://www.pbc.gov.cn/tiaofasi/144941/3581332/index.html"


def make_doc(title: str, url: str, published_on: date | None = None) -> RawDoc:
    """构造测试用 ``RawDoc``，减少重复样板代码。"""
    # 组装条目，source_id 固定为测试标识
    return RawDoc(title=title, url=url, source_id="test", published_on=published_on)


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
