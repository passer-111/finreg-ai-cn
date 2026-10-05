"""抓取器离线端到端测试 —— 全程不触网。

为什么必须是端到端而非只测单元
------------------------------
本项目的多个关键缺陷都出在「各部件单独看都对，拼起来不对」的地方：
- 日期字段映射指向了 epoch 整数，单元测试全过，整条链路丢日期
- PbcFetcher 直接继承基类，解析逻辑对，但分页与去重没生效
- CsrcFetcher 抓到了页面，但抓到的是导航而非规章

因此这些测试从 ``fetch()`` 这个对外入口开始，经过分页、解析、质量自检、
去重、导航剔除、关键词过滤，一直到最终的 ``FetchResult``，
断言的是**使用者真正看到的东西**。
"""

# 导入 json 用于构造伪造的接口响应
import json

# 导入 BeautifulSoup 用于在测试中篡改页面结构（验证「按表头定位列」）
from bs4 import BeautifulSoup

# 导入待测抓取器与工具
from finreg_ai.fetchers.cac import CacFetcher
from finreg_ai.fetchers.csrc import CsrcFetcher, extract_meta
from finreg_ai.fetchers.html_list import HtmlListFetcher
from finreg_ai.fetchers.json_search_list import JsonSearchListFetcher
from finreg_ai.fetchers.pbc import PbcFetcher
from finreg_ai.fetchers.penalty_table import PenaltyTableFetcher

# 从共享装置导入伪造会话、固定装置读取器与数据源构造器
from tests.conftest import (
    FakeResponse,
    FakeSession,
    load_fixture_json,
    load_fixture_text,
    make_source,
)

# 人行规范性文件列表页地址，与真实配置一致
PBC_LIST_URL = "http://www.pbc.gov.cn/tiaofasi/144941/3581332/index.html"

# 证监会列表页地址（需要带 channelid，真实配置即如此）
CSRC_LIST_URL = "https://www.csrc.gov.cn/csrc/c101953/common_list.shtml?channelid=test-channel"

# 人民银行行政处罚公示列表页地址，与 data/sources.yaml 一致
PBC_PENALTY_LIST_URL = (
    "https://www.pbc.gov.cn/zhengwugongkai/4081330/4081344/4081407/4081705/index.html"
)

# 机构站点自检所需的 meta 标签片段，照搬证监会页面 <head> 的写法
CSRC_META_HTML = (
    '<!DOCTYPE html><html><head>'
    '<meta name="SiteName" content="中国证券监督管理委员会">'
    '<meta name="ColumnName" content="规章">'
    '</head><body></body></html>'
)

# 只有导航链接、没有任何政策条目的列表页，用于测试「全被清洗掉」时的降级
NAV_ONLY_HTML = (
    '<html><body><ul>'
    '<li><a href="/kejisi/146812/index.html">金融科技</a></li>'
    '<li><a href="/zhengwugongkai/index.html">政务公开</a></li>'
    '</ul></body></html>'
)


# ============================================================
# 测试数据源构造辅助
# ============================================================

def _source_from(mapping: dict):
    """从字典构造数据源，把字典展开为关键字参数。

    存在的意义：下面多处测试需要构造「大部分字段与真实配置一致、
    只改一两处」的数据源，用字典字面量写更易读。
    """
    # 展开字典后交给共享装置
    return make_source(**mapping)


def make_json_source(**overrides: object):
    """构造使用 json_search_list 抓取器的数据源，默认接口配置与 NFRA 一致。"""
    # 默认使用金融监管总局的接口配置
    defaults: dict = {"fetcher": "json_search_list", "api": _nfra_api_config()}
    # 应用覆盖项
    defaults.update(overrides)
    # 交给共享装置构造
    return make_source(**defaults)


def make_html_source(**overrides: object):
    """构造使用 html_list 抓取器的人行风格数据源。"""
    # 默认选择器照搬 data/sources.yaml 中 pbc-regulations 的真实配置
    defaults: dict = {
        "fetcher": "html_list",
        "list_url": PBC_LIST_URL,
        "selectors": {
            "item": "table tr",
            "title": "a",
            "link": "a",
            "date": "td:last-child",
        },
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 交给共享装置构造
    return make_source(**defaults)


# ============================================================
# 通用 JSON 抓取器 —— 以金融监管总局的接口形态为样本
# ============================================================

def _nfra_api_config(**overrides: object) -> dict:
    """构造与 nfra-regulations 一致的 api 配置，可用关键字覆盖任意字段。"""
    # 基线配置照搬 data/sources.yaml 中的真实值
    config: dict = {
        "path": "/cbircweb/DocInfo/SelectDocByItemIdAndChild",
        "method": "GET",
        "params": {
            "itemId": "915",
            "pageSize": "{page_size}",
            "pageIndex": "{page}",
        },
        "items_path": "data.rows",
        "total_path": "data.total",
        "url_template": "https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId={docId}",
        "field_map": {"title": "docSubtitle", "date": "publishDate"},
        "page_size": 3,
    }
    # 应用覆盖项
    config.update(overrides)
    # 返回配置
    return config


def _nfra_router(url: str) -> FakeResponse:
    """按 URL 中的 pageIndex 路由：第 1 页返回固定装置，其余页返回空结果。"""
    # 第 1 页返回固定装置
    if "pageIndex=1" in url:
        # 返回装置内容
        return FakeResponse(text=load_fixture_text("nfra_doc_list.json"))
    # 其余页返回空的条目数组，用于触发「已到末页」的终止逻辑
    return FakeResponse(text=json.dumps({"rptCode": 200, "data": {"total": 3, "rows": []}}))


def test_json_fetcher_parses_and_paginates_offline() -> None:
    """验证 JSON 抓取器能从固定装置解析出条目，并按末页判断终止翻页。"""
    # 构造数据源
    source = make_json_source()
    # 注入伪造会话
    session = FakeSession(_nfra_router)
    # 构造抓取器
    fetcher = JsonSearchListFetcher(source, session=session)

    # 执行抓取
    result = fetcher.fetch()

    # 状态应为 ok
    assert result.status == "ok"
    # 固定装置中有 3 条记录
    assert len(result.docs) == 3
    # 应发了 2 次请求：第 1 页有数据，第 2 页为空即终止
    assert result.request_count == 2
    # 标题取自 docSubtitle（单行字段），而非内嵌换行的 docTitle
    assert result.docs[0].title.startswith("某监管机构发布《关于银行业保险业人工智能安全开发应用的指导意见")
    # 标题中不应残留换行符
    assert "\n" not in result.docs[0].title


def test_json_fetcher_parses_date_from_readable_string() -> None:
    """验证可读日期字符串被正确解析。"""
    # 构造并执行抓取
    result = JsonSearchListFetcher(make_json_source(), session=FakeSession(_nfra_router)).fetch()
    # 第一条的日期应为装置中的 2026-06-18
    assert result.docs[0].published_on is not None
    # 断言具体日期
    assert result.docs[0].published_on.isoformat() == "2026-06-18"


def test_json_fetcher_renders_detail_url_from_doc_id() -> None:
    """验证在接口不返回链接字段时，用 url_template 从 docId 拼出详情页地址。"""
    # 执行抓取
    result = JsonSearchListFetcher(make_json_source(), session=FakeSession(_nfra_router)).fetch()
    # 应拼出带 docId 的详情页地址
    assert result.docs[0].url == "https://www.nfra.gov.cn/cn/view/pages/ItemDetail.html?docId=900001"


# ============================================================
# 关键词过滤的丢弃明细 —— 「计数」之外还要留下「被丢的是谁」
# ------------------------------------------------------------
# 固定装置里有三条：两条含「人工智能」，一条是
# 《关于某年度银行业保险业主要监管指标数据情况的通报》。
# 这恰好能同时构造出「命中包含词」「未命中包含词」「命中排除词」三种命运，
# 不需要另造装置。
# ============================================================

def test_fetch_leaves_no_dropped_docs_when_no_filters_configured() -> None:
    """验证未配置过滤规则时，丢弃明细为空。

    防止一种误报：没有任何过滤却报告「丢弃了 N 条」，
    会让人去追一个不存在的配置问题。
    """
    # 显式不给 filters
    source = make_json_source(filters=None)
    # 抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 三条全部保留
    assert len(result.docs) == 3
    # 无丢弃
    assert result.dropped_by_filter == 0
    # 丢弃明细也应为空
    assert result.dropped_filter_docs == []


def test_fetch_dropped_docs_length_matches_dropped_count() -> None:
    """验证「丢弃明细条数 == 丢弃计数」这条不变式。

    两者一旦能对不上，就说明差集算法有问题（例如漏掉了 URL 重复的情况），
    而那种缺陷的表现恰好是「报告了数量但列不出对应条目」——
    看起来一切正常，复核时才发现少了东西。因此必须用不变式钉住。
    """
    # 只保留含「人工智能」的条目
    source = make_json_source(filters={"include_keywords": ["人工智能"], "exclude_keywords": []})
    # 抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 三条里保留两条
    assert len(result.docs) == 2
    # 丢弃一条
    assert result.dropped_by_filter == 1
    # 明细条数必须与计数一致
    assert len(result.dropped_filter_docs) == result.dropped_by_filter
    # 而且丢掉的正是那条不相关的监管指标通报
    assert "监管指标" in result.dropped_filter_docs[0].title


def test_fetch_dropped_docs_distinguishes_excluded_from_not_matched() -> None:
    """验证同一批条目按不同原因被丢弃时，明细与原因计数能对上。

    固定装置里那条「监管指标数据情况通报」恰好同时：
    - 不含「人工智能」→ 本会因未命中被丢
    - 含「监管指标」→ 也会因命中排除词被丢
    排除规则优先，因此原因是 excluded 而不是 not_matched。
    这条测试正是要钉住这个优先级在**明细层面**也成立。
    """
    # 包含「人工智能」，排除「监管指标」
    source = make_json_source(
        filters={"include_keywords": ["人工智能"], "exclude_keywords": ["监管指标"]}
    )
    # 抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 保留两条
    assert len(result.docs) == 2
    # 原因是「命中排除词」而非「未命中包含词」
    assert result.filter_drop_reasons == {"excluded": 1}
    # 明细里那一条正是监管指标通报
    assert len(result.dropped_filter_docs) == 1
    # 标题匹配
    assert "监管指标" in result.dropped_filter_docs[0].title


def test_fetch_exposes_dropped_docs_even_when_status_is_ok() -> None:
    """反向测试：状态为 ok 且仍有条目时，丢弃明细照样必须留存。

    这是最容易被「优化」掉的一条：既然抓到了东西，何必再留着被丢掉的？
    但实测 nfra-regulations 每次保留 5 条、丢弃 155 条、状态完全正常——
    若不留下明细，这 155 条里可能含有的重要政策永远无人有机会发现。
    """
    # 只保留含「人工智能」的
    source = make_json_source(filters={"include_keywords": ["人工智能"]})
    # 抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 状态正常
    assert result.status == "ok"
    # 且确实抓到了条目
    assert result.docs
    # 但被丢弃的条目依然留在了结果里
    assert result.dropped_filter_docs
    # 每一条都有标题可供复核
    assert all(d.title for d in result.dropped_filter_docs)


# ============================================================
# 行政处罚抓取器 —— 列表页只给文号，真实内容在详情页表格里
# ------------------------------------------------------------
# 这组测试用真实页面（2026-10-05 抓取后存为装置）驱动，全程离线。
#
# 为什么值得写这么多条
# --------------------
# 罚单抓取器的失败方式与其它抓取器不同：它的列表页标题只有文号，
# 关键词过滤在那里完全失效；真实内容在另一个页面的一张表格里。
# 一旦表格解析错位，得到的不是「0 条」或报错，而是
# **一批看起来很正常、但字段彼此错位的记录**——
# 比如把「公示期限」当成「决定机关」。这种错误在合规场景中最危险，
# 因为记录本身读起来毫无破绽。
# ============================================================

def _penalty_source(**overrides: object):
    """构造罚单源，配置照搬 data/sources.yaml。"""
    # 默认配置
    defaults: dict = {
        "id": "pbc-administrative-penalties",     # 源标识
        "issuer_code": "pbc",                     # 关联机构
        "fetcher": "penalty_table",               # 抓取器
        "list_url": PBC_PENALTY_LIST_URL,         # 列表页地址
        "selectors": {                            # 列表页选择器
            "item": "ul.txtlist li",
            "title": "a",
            "link": "a",
            "date": "span",
        },
        # 详情页表格配置：只开 2 个详情页，测试不必跑满
        "penalty": {"row": "table tr", "max_details": 2},
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 构造
    return make_source(**defaults)


def _penalty_router(url: str) -> FakeResponse:
    """按 URL 路由到罚单固定装置。"""
    # 列表页
    if url.endswith("4081705/index.html"):
        # 返回列表页装置
        return FakeResponse(text=load_fixture_text("pbc_penalty_list.html"))
    # 早一期的那份文书
    if "5860077" in url:
        # 返回旧文书装置
        return FakeResponse(text=load_fixture_text("pbc_penalty_detail_old.html"))
    # 其余返回最新那份文书
    return FakeResponse(text=load_fixture_text("pbc_penalty_detail.html"))


def _penalty_router_with_detail(detail_html: str):
    """构造一个「列表页用真实装置、所有详情页都用给定 HTML」的路由。

    为什么需要它，而不是直接写 ``FakeSession(lambda url: FakeResponse(text=x))``
    -----------------------------------------------
    后者是把**整站**响应换成了那个 HTML，列表页也不例外。
    于是列表页解析出 0 条，抓取在「列表阶段」就降级了，
    连一个详情页都不会打开——测试看似跑通，实际断言的是
    「列表页拿不到东西所以降级」，与本测试想验证的详情页行为毫无关系。
    这类「反向测试其实没有触发反向路径」的失误比没有测试更糟：
    它给人一个已经防住了的错觉。所以列表页必须照常返回真实装置。
    """
    def router(url: str) -> FakeResponse:
        """路由函数。"""
        # 列表页照常返回真实装置
        if url.endswith("4081705/index.html"):
            # 返回列表页装置
            return FakeResponse(text=load_fixture_text("pbc_penalty_list.html"))
        # 详情页一律返回调用方给定的 HTML
        return FakeResponse(text=detail_html)

    # 返回路由函数
    return router


def test_penalty_fetcher_extracts_one_row_per_party() -> None:
    """验证一份公示文书里的多个当事人被拆成多条记录。

    这是罚单数据的基本形态：一份「银罚决字〔2026〕104-116号」覆盖 13 个当事人
    （1 家机构 + 12 名责任人）。若整份文书只产出一条记录，
    「谁被罚了、因为什么」这个最基本的问题就答不上来。

    这里必须把 ``max_details`` 显式限成 1。原因不是省事，而是断言本身
    是「**一份**文书 → 13 条」：列表页有 20 份文书，若默认打开 2 个详情页，
    路由会把最新那份文书喂给两个地址，于是同一批 13 行被读两遍，
    得到 26 条——数字翻倍但没有任何字段出错，看起来像「抓得更全了」。
    这类错误只有把断言的前提（几份文书）钉死才拦得住。
    """
    # 抓取：只开 1 个详情页，等于只处理 1 份文书
    source = _penalty_source(penalty={"row": "table tr", "max_details": 1})
    # 执行
    result = PenaltyTableFetcher(source, session=FakeSession(_penalty_router)).fetch()
    # 状态正常
    assert result.status == "ok"
    # 一份文书里有 13 个当事人，因此应产出 13 条
    assert len(result.docs) == 13
    # 每条都有当事人
    assert all(d.extra.get("party") for d in result.docs)
    # 当事人名互不相同（即确实按行拆开了，而不是把同一行复制了 13 次）
    assert len({d.extra["party"] for d in result.docs}) == 13


def test_penalty_fetcher_copies_official_fields_verbatim() -> None:
    """验证字段是从官方表格逐字抄录的，没有改写、没有解析。

    这里断言的是**具体字符串**而不是「字段非空」。理由：
    本抓取器唯一的职责就是原样搬运；一旦有人为了「结构化」把
    「2026年9月8日」转成 date、或把「警告，没收违法所得1.621747万元，
    罚款1712.4万元」拆成金额数字，记录看起来会更整齐，
    但原文的表述方式（处罚种类的组合、是否写明具体数额）就丢了，
    而这些恰恰是判断执法尺度的依据。
    """
    # 抓取
    result = PenaltyTableFetcher(_penalty_source(), session=FakeSession(_penalty_router)).fetch()
    # 第一条即广发银行那条
    first = result.docs[0].extra
    # 当事人与官方原文一致
    assert first["party"] == "广发银行股份有限公司"
    # 文号一致
    assert first["decision_no"] == "银罚决字〔2026〕104号"
    # 处罚内容一致（保留原文的金额写法，未被转成数字）
    assert first["penalty_content"] == "警告，没收违法所得1.621747万元，罚款1712.4万元"
    # 决定机关一致
    assert first["authority"] == "中国人民银行"
    # 决定日期保留官方中文写法，未被转成 ISO 格式
    assert first["decision_date"] == "2026年9月8日"
    # 公示期限一致
    assert first["publicity_period"] == "五年"
    # 违规事实含「违反数据安全管理规定」——这是本项目收录罚单的核心价值
    assert "违反数据安全管理规定" in first["violation_type"]


def test_penalty_fetcher_builds_title_from_party_and_violation() -> None:
    """验证标题由「当事人｜违规事由」重建，而不是沿用文号。

    为什么这件事重要：若标题是文号（「银罚决字〔2026〕104号」），
    那么「数据安全」「人工智能」这类关键词一条都命中不了，
    罚单数据在检索层面等于不存在。

    断言的是**截断窗口内**的词（「违反金融统计管理规定」在原文第 3 字起）。
    为什么不直接断言「违反数据安全管理规定」：它在原文第 59 字起，
    正好被 60 字的截断切在词中间，标题里只剩「违反数…」。
    这不是缺陷，而是截断本就该有的样子——标题负责可读。
    落在窗口之外的关键词由下面那条测试负责保证不丢。
    """
    # 抓取
    result = PenaltyTableFetcher(_penalty_source(), session=FakeSession(_penalty_router)).fetch()
    # 取第一条
    title = result.docs[0].title
    # 标题以当事人开头
    assert title.startswith("广发银行股份有限公司｜")
    # 标题含违规事由的关键字
    assert "违反金融统计管理规定" in title
    # 标题不含文号——文号只应存在于 extra 里，不应混进标题
    assert "银罚决字" not in title
    # 违规事由被截断时带省略号，让人一眼看出这不是全文
    assert title.endswith("…")


def test_penalty_fetcher_keeps_rows_whose_keyword_falls_past_the_truncation() -> None:
    """反向测试：关键词落在标题截断点之后时，记录必须仍被保留。

    这是本项目最典型的一类「看不见的过滤器」，而且极难发现：

    实数据里「数据安全」出现在违规事由的第 59 个字符处，
    标题截断到 60 字后只剩「违反数…」——**词被切了一半**。
    若关键词过滤只看标题，这条记录会被判为不相关而丢弃，
    抓取报告依然显示 ok、丢弃计数显示「未命中包含词」，
    看上去完全正常；而人工复核时盯着「违反数…」也不会觉得少了什么。

    因此本测试用一个只含「数据安全」的包含词表来验证：
    过滤必须能从 ``extra["violation_type"]``（未截断原文）里看见它。
    若有人把过滤改回「只看标题」，这条会立刻失败。
    """
    # 只留一个必然落在截断点之后的关键词
    source = _penalty_source(
        filters={"include_keywords": ["数据安全"], "exclude_keywords": []},
        penalty={"row": "table tr", "max_details": 1},
    )
    # 抓取
    result = PenaltyTableFetcher(source, session=FakeSession(_penalty_router)).fetch()
    # 确认前提成立：标题里确实看不到这个词（否则本测试没有测到东西）
    assert all("数据安全" not in d.title for d in result.docs)
    # 但机构那一条必须被保下来
    assert any("数据安全" in d.extra["violation_type"] for d in result.docs)
    # 且它通过了过滤，没有被计入「未命中包含词」
    assert result.dropped_by_filter == 12  # 13 条里只有机构那条含「数据安全」
    # 保下来的正是机构那条
    assert result.docs[0].extra["party"] == "广发银行股份有限公司"


def test_penalty_fetcher_keeps_decision_no_in_extra() -> None:
    """验证文号虽不在标题里，但必须保留在结构化字段中。

    文号是唯一能把「同一份文书下的多个当事人」重新聚起来的线索，
    也是回查官网原文的检索词。为了标题好读而丢掉它，
    会让复核工作多一道无谓的障碍。
    """
    # 抓取
    result = PenaltyTableFetcher(_penalty_source(), session=FakeSession(_penalty_router)).fetch()
    # 每条都有文号
    assert all(d.extra.get("decision_no") for d in result.docs)
    # 且文号形式正确（该批均为「银罚决字〔年份〕序号号」）
    assert all("银罚决字" in d.extra["decision_no"] for d in result.docs)
    # 同时保留了所属文书地址，便于回源
    assert all(d.extra.get("document_url") for d in result.docs)


def test_penalty_fetcher_row_url_is_unique_per_party() -> None:
    """验证同文书内的每条记录地址互不相同。

    若都用文书地址，``dedupe_by_url`` 会把同一份文书下的 13 条记录
    去重成 1 条——而抓取报告只会显示「条目数=1」，
    看起来一切正常。用「文书地址 + 文号」做行级地址即可避免。
    """
    # 抓取
    result = PenaltyTableFetcher(_penalty_source(), session=FakeSession(_penalty_router)).fetch()
    # 地址数量与记录数一致
    assert len({d.url for d in result.docs}) == len(result.docs)
    # 且每条地址都带文号片段
    assert all("银罚决字" in d.url for d in result.docs)


def test_penalty_fetcher_locates_columns_by_header_not_position() -> None:
    """反向测试：打乱表格列顺序后仍能正确解析。

    这是本抓取器最重要的一条测试。它钉住的设计决定是
    「按表头文字定位列，而不是按列序号」。

    为什么必须这样：列序号是排版细节，会随改版变化；表头文字是这一列的
    身份。若按序号取值，监管把「公示期限」挪到「备注」之后时，
    解析不会报任何错，只会把公示期限读成备注——**字段悄悄错位**。
    这类错误在合规场景中最危险，因为记录读起来毫无破绽。

    构造方式：把原文表格的列顺序完全颠倒，再断言关键字段仍落在正确的位置。
    """
    # 取真实详情页，把表头与数据行的列顺序颠倒
    scrambled = _scramble_penalty_table(load_fixture_text("pbc_penalty_detail.html"))
    # 构造路由：列表页仍是真实装置，详情页一律返回颠倒后的页面
    session = FakeSession(_penalty_router_with_detail(scrambled))
    # 抓取（只开 1 个详情页）
    source = _penalty_source(penalty={"row": "table tr", "max_details": 1})
    # 执行
    result = PenaltyTableFetcher(source, session=session).fetch()
    # 先确认前提：确实走到了详情页解析，而不是在列表阶段就降级了
    assert result.status == "ok", f"未走到详情页解析：{result.error}"
    # 仍应解析出记录
    assert result.docs, "列顺序被颠倒后一条记录都没解析出来"
    # 关键：字段内容仍然正确，没有被错位替换
    first = result.docs[0].extra
    # 当事人仍是机构名
    assert first["party"] == "广发银行股份有限公司"
    # 处罚内容仍是处罚而非日期
    assert "罚款" in first["penalty_content"]
    # 决定机关仍是「中国人民银行」
    assert first["authority"] == "中国人民银行"
    # 公示期限仍是「五年」而非某个日期
    assert first["publicity_period"] == "五年"


def test_penalty_fetcher_fails_loudly_when_no_table_can_be_parsed() -> None:
    """反向测试：详情页完全没有表格时，必须报错而不是静默返回空。

    返回空会被上层判为「成功但 0 条」，于是「表格选择器失效」
    会伪装成「今天没有新处罚」——而罚单本来就是低频更新，
    「今天没有」听起来完全合理，因此这种静默失效可能持续数月无人发现。
    """
    # 列表页照常可用；所有详情页都返回一个没有表格的页面
    session = FakeSession(
        _penalty_router_with_detail("<html><body><p>页面改版了</p></body></html>")
    )
    # 执行
    result = PenaltyTableFetcher(_penalty_source(), session=session).fetch()
    # 必须失败，而不是 ok
    assert result.status == "failed"
    # 错误信息要指出排查方向（配置项名称），而不只是「失败了」
    assert "penalty.row" in (result.error or "")
    assert "penalty.columns" in (result.error or "")


def test_penalty_fetcher_drops_navigation_before_opening_details() -> None:
    """反向测试：导航链接必须在打开详情页之前就被剔除。

    这是一个真实踩过的坑：基类确实会在 ``_collect()`` 返回后清洗导航，
    但那已经太晚——本抓取器会先为列表里的每一条打开详情页，
    于是导航项把 ``max_details`` 配额全部吃光，20 个详情页全是
    「政府信息公开年报」这类栏目页，一页表格也解析不出来。

    断言方式直接盯住「请求了哪些地址」：若导航项被打开过，
    它会出现在 session.calls 里。
    """
    # 构造一个「导航项在前、真实文书在后」的列表页
    list_html = _penalty_list_with_navigation()
    # 路由：列表页返回合成页面，其余返回真实详情页
    def router(url: str) -> FakeResponse:
        """路由函数。"""
        # 列表页
        if url.endswith("4081705/index.html"):
            # 返回合成列表页
            return FakeResponse(text=list_html)
        # 其余返回真实详情页
        return FakeResponse(text=load_fixture_text("pbc_penalty_detail.html"))

    # 只开 1 个详情页——若导航未被提前剔除，这唯一的配额会被导航吃掉
    source = _penalty_source(penalty={"row": "table tr", "max_details": 1})
    # 构造会话
    session = FakeSession(router)

    # 前置检查：导航项必须能通过列表解析（否则它到不了导航清洗这一步，
    # 本测试会因「导航数=0」而失败得莫名其妙）。这一步把失败原因说清楚。
    listed = HtmlListFetcher(source, session=session).parse(list_html)
    # 合成页面里两条都应被保留：一条导航、一条真实文书
    assert len(listed) == 2, (
        f"合成列表页只解析出 {len(listed)} 条，导航项未通过列表解析的守卫，"
        "本测试的前提不成立（请检查导航项标题是否短于 4 字）"
    )

    # 执行
    result = PenaltyTableFetcher(source, session=session).fetch()

    # 应正常解析出记录（说明配额用在了真实文书上）
    assert result.status == "ok"
    assert result.docs
    # 被剔除的导航数不为 0，且这个数字会并入报告的 dropped_navigation
    assert result.dropped_navigation >= 1
    # 决定性断言：导航页从未被请求过
    requested = " ".join(url for _method, url in session.calls)
    # 合成列表页里的导航项地址特征是 4081347（政府信息公开年报栏目）
    assert "4081347" not in requested, "导航链接在打开详情页之后才被剔除，配额被浪费了"


def test_fetch_force_bypasses_disabled_source() -> None:
    """验证 force=True 能运行 enabled=False 的源，且默认行为不变。

    这条能力存在的理由：`enabled: false` 表达的是「不要放进每日流水线」，
    而不是「永远不许运行」。行政处罚源正是这种情况——它每次产出数百条，
    进了变更流会淹没真正的政策变化，因此常年停用；但维护者仍需要
    主动去拉一次。若没有 force，就只能改 YAML → 跑 → 改回来，
    而「忘了改回来」会让一个已知有问题的源重新进入每日流水线。
    """
    # 一个被停用的源
    disabled = _penalty_source(enabled=False)
    # 默认行为：跳过
    skipped = PenaltyTableFetcher(disabled, session=FakeSession(_penalty_router)).fetch()
    # 状态应为 skipped
    assert skipped.status == "skipped"
    # 且没有发起任何请求
    assert skipped.request_count == 0
    # force=True：正常抓取
    forced = PenaltyTableFetcher(disabled, session=FakeSession(_penalty_router)).fetch(force=True)
    # 应拿到数据
    assert forced.status == "ok"
    assert forced.docs


def _scramble_penalty_table(html: str) -> str:
    """把详情页表格的列顺序整体颠倒，用于验证「按表头定位列」。

    只动列的顺序，不改单元格内容——这样若解析仍正确，说明定位依据是表头文字；
    若解析结果错位，说明定位依据是列序号。
    """
    # 解析
    soup = BeautifulSoup(html, "lxml")
    # 找到第一个表格
    table = soup.find("table")
    # 表格不存在时直接返回原文（调用方会因此断言失败，属于预期）
    if table is None:
        # 原样返回
        return html
    # 逐行颠倒单元格顺序（表头行与数据行一并处理，保证两者仍对齐）
    for tr in table.find_all("tr"):
        # 取出本行单元格
        cells = tr.find_all(["th", "td"])
        # 空行跳过
        if not cells:
            # 下一行
            continue
        # 逐个摘出（extract 会从树上移除，但不破坏内容）
        for cell in cells:
            # 摘出该单元格
            cell.extract()
        # 反向插回，实现整行倒序
        for cell in reversed(cells):
            # 追加到行末
            tr.append(cell)
    # 返回篡改后的 HTML
    return str(soup)


def _penalty_list_with_navigation() -> str:
    """构造一个「导航项在前、真实文书在后」的罚单列表页。

    真实页面里导航与文书分属不同容器，因此当前选择器命中不了导航。
    这个合成页面刻意让导航项也落在 ``ul.txtlist`` 里，
    用于验证「即使选择器退化、导航清洗也能兜住」这层防护是否真的生效。

    导航项的标题写「政府信息公开年报」而不是「年报」，是有原因的：
    列表解析器有一条「标题短于 4 字即跳过」的守卫，两字标题会在**解析阶段**
    就被丢掉，根本到不了导航清洗。测试于是变成空转——
    它断言 ``dropped_navigation >= 1``，而那 0 是因为标题太短，
    不是因为导航清洗失效。这个坑真的踩过一次。
    """
    # 返回合成 HTML
    return (
        "<html><body>"
        "<ul class='txtlist'>"
        # 导航项：标题短（8 字，不触发「短于 4 字」守卫、又在 12 字上限内）、
        # 路径浅（比列表页少两层），应被导航启发式剔除
        "<li><a href='/zhengwugongkai/4081330/4081347/index.html'>政府信息公开年报</a>"
        "<span>·</span></li>"
        # 真实文书：标题长、路径更深
        "<li><a href='https://www.pbc.gov.cn/zhengwugongkai/4081330/4081344/4081407/4081705/2026092418294123383/index.html'>"
        "银罚决字〔2026〕104-116号</a><span>2026-09-24</span></li>"
        "</ul>"
        "</body></html>"
    )


def test_json_fetcher_drops_rows_when_template_field_missing() -> None:
    """验证模板所需字段缺失时该条被丢弃，而不是生成残缺链接。"""
    # 把模板改成一个装置中不存在的字段
    api = _nfra_api_config(url_template="https://a.cn/detail?docId={notExistField}")
    # 构造数据源
    source = make_json_source(api=api)
    # 执行抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 三条都渲染失败 → 空结果 → 降级，而不是 ok
    assert result.status == "degraded"
    # 结果中不应有任何条目
    assert result.docs == []


def test_json_fetcher_quality_check_rejects_when_date_field_is_wrong() -> None:
    """验证日期字段映射失效时被质量自检拦下并降级。

    这条测试对应本项目真实踩过的坑：证监会接口的日期在 publishedTime
    （epoch 毫秒）里，若映射到不存在的字段，所有条目都会静默丢失日期，
    而流水线仍然报告 ok。质量自检就是为了拦住这种情况。
    """
    # 把日期映射指向一个不存在的字段
    api = _nfra_api_config(field_map={"title": "docSubtitle", "date": "noSuchDateField"})
    # 构造数据源
    source = make_json_source(api=api)
    # 执行抓取
    result = JsonSearchListFetcher(source, session=FakeSession(_nfra_router)).fetch()
    # 应降级而非 ok
    assert result.status == "degraded"
    # 错误说明应指出是质量自检未通过
    assert result.error is not None
    # 断言说明中包含可定位的关键词
    assert "质量自检" in result.error


def test_json_fetcher_reports_http_status_when_request_fails() -> None:
    """验证接口返回非 200 时，错误说明里带上具体状态码。

    这条测试对应另一个真实缺陷：nfra 抓取器曾用裸 except 吞掉异常，
    导致降级时只显示「未解析到任何条目」这种无法定位的通用文案。
    """
    # 路由始终返回 404
    def router(_url: str) -> FakeResponse:
        """始终返回 404，模拟接口地址失效。"""
        # 返回 404 响应
        return FakeResponse(text="<html>404 Not Found</html>", status_code=404)

    # 构造数据源与抓取器
    source = make_json_source()
    # 执行抓取
    result = JsonSearchListFetcher(source, session=FakeSession(router)).fetch()

    # 首页请求失败属明确失败
    assert result.status == "failed"
    # 错误说明中必须出现状态码，否则运维只能靠猜
    assert result.error is not None
    # 断言包含 404
    assert "404" in result.error


def test_json_fetcher_reports_non_json_response() -> None:
    """验证接口返回 HTML 而非 JSON 时（被重定向的典型征兆）被明确报告。"""
    # 路由返回 HTML
    def router(_url: str) -> FakeResponse:
        """返回 HTML 内容，模拟被重定向到错误页。"""
        # 返回 HTML
        return FakeResponse(text="<html><body>请稍后再试</body></html>", status_code=200)

    # 执行抓取
    result = JsonSearchListFetcher(make_json_source(), session=FakeSession(router)).fetch()
    # 应为明确失败
    assert result.status == "failed"
    # 错误说明应说明响应不是 JSON
    assert result.error is not None
    # 断言关键词
    assert "非 JSON" in result.error


# ============================================================
# 通用 HTML 抓取器 —— 以人行 table 布局为样本
# ============================================================

def test_html_fetcher_parses_table_layout_offline() -> None:
    """验证通用 HTML 抓取器能解析 table 布局并提取日期。

    这条测试纠正了本项目的一个早期误判：曾认为人行页面
    「63 个 table tr 全部是布局表格，不含政策列表项」，
    实际是表格里混了导航噪声，真实政策条目是可以解析出来的。
    """
    # 路由始终返回人行页面固定装置
    def router(_url: str) -> FakeResponse:
        """返回人行列表页固定装置。"""
        # 返回 HTML
        return FakeResponse(text=load_fixture_text("pbc_common_table.html"), encoding="gb2312")

    # 构造抓取器（不分页，避免伪造会话无限返回同一页）
    source = make_html_source(pagination={"enabled": False, "max_pages": 1, "url_pattern": None})
    # 执行抓取
    result = HtmlListFetcher(source, session=FakeSession(router)).fetch()

    # 应成功
    assert result.status == "ok"
    # 装置中有 3 条真实政策（公告 + 金融科技发展规划 + 数据治理通知）
    assert len(result.docs) == 3
    # 每一条都应解析出日期
    assert all(doc.published_on is not None for doc in result.docs)
    # 应剔除了 5 条导航链接（新闻发布/政务公开/网送文告/金融科技/法律声明）
    assert result.dropped_navigation == 5


# ============================================================
# 人行专用抓取器 —— 导航污染的关键回归测试
# ============================================================

def test_pbc_fetcher_removes_column_navigation_pollution() -> None:
    """验证「金融科技」这类栏目导航链接不会进入结果。

    这是本项目最隐蔽的一类污染：栏目名含关键词 → 骗过关键词过滤 →
    成为唯一命中项 → 流水线在 0 条真实政策时报 ok。
    本测试是该缺陷的回归测试。
    """
    # 路由返回人行固定装置
    def router(_url: str) -> FakeResponse:
        """返回人行列表页固定装置。"""
        # 返回 HTML
        return FakeResponse(text=load_fixture_text("pbc_common_table.html"), encoding="gb2312")

    # 构造 pbc 抓取器对应的数据源
    source = _source_from(
        {
            "fetcher": "pbc",
            "list_url": PBC_LIST_URL,
            "pagination": {"enabled": False, "max_pages": 1, "url_pattern": None},
            "selectors": {"item": "table tr", "title": "a", "link": "a", "date": "td:last-child"},
            # 过滤规则包含「金融科技」，正是诱导污染的条件
            "filters": {"include_keywords": ["人工智能", "金融科技", "科技", "数据"], "exclude_keywords": []},
        }
    )
    # 执行抓取
    result = PbcFetcher(source, session=FakeSession(router)).fetch()

    # 结果中不得出现「金融科技」这个栏目名
    titles = [doc.title for doc in result.docs]
    # 断言导航链接已被剔除
    assert "金融科技" not in titles
    # 命中项应是含「金融科技」字样的真实文件标题（而非栏目名本身）
    assert all(len(title) > 12 for title in titles)
    # 剔除数量应被如实报告出来，而不是静默清洗
    assert result.dropped_navigation >= 1


def test_pbc_fetcher_filters_pagination_noise() -> None:
    """验证「下一页」「尾页」等分页文字不会成为条目标题。"""
    # 路由返回人行固定装置
    def router(_url: str) -> FakeResponse:
        """返回人行列表页固定装置。"""
        # 返回 HTML
        return FakeResponse(text=load_fixture_text("pbc_common_table.html"), encoding="gb2312")

    # 构造数据源
    source = _source_from(
        {
            "fetcher": "pbc",
            "list_url": PBC_LIST_URL,
            "pagination": {"enabled": False, "max_pages": 1, "url_pattern": None},
            "selectors": {"item": "table tr", "title": "a", "link": "a", "date": "td:last-child"},
        }
    )
    # 执行抓取
    result = PbcFetcher(source, session=FakeSession(router)).fetch()

    # 结果中不得包含分页导航文字
    for doc in result.docs:
        # 逐条断言
        assert doc.title not in {"下一页", "尾页", "首页", "末页"}


# ============================================================
# 证监会抓取器 —— 站点自检
# ============================================================

def test_extract_meta_reads_meta_content() -> None:
    """验证能从 HTML 中提取指定 meta 标签的 content 值。"""
    # 从固定片段中提取站点名
    assert extract_meta(CSRC_META_HTML, "SiteName") == "中国证券监督管理委员会"
    # 提取栏目名
    assert extract_meta(CSRC_META_HTML, "ColumnName") == "规章"


def test_extract_meta_returns_none_when_absent() -> None:
    """验证 meta 标签不存在时返回 None。"""
    # 不存在的 meta 名
    assert extract_meta(CSRC_META_HTML, "NoSuchMeta") is None


def test_csrc_fetcher_fails_when_site_check_fails() -> None:
    """验证站点自检失败时该源明确标为失败，而不是带着错误内容「成功」返回。

    这是刻意设计的选择：政府网站改版后旧 URL 常被重定向到首页而非 404。
    此时抓取器会「成功地」抓到首页导航文字。站点自检能把这一步拦住。
    """
    # 路由返回不含 SiteName 的 HTML（模拟被重定向到非预期页面）
    def router(_url: str) -> FakeResponse:
        """返回一个不含所需 meta 的页面。"""
        # 返回无关键 meta 的 HTML
        return FakeResponse(text="<html><head></head><body>首页</body></html>")

    # 构造数据源
    source = _source_from(
        {"fetcher": "csrc", "list_url": CSRC_LIST_URL, "api": _nfra_api_config()}
    )
    # 执行抓取
    result = CsrcFetcher(source, session=FakeSession(router)).fetch()

    # 应为明确失败
    assert result.status == "failed"
    # 错误说明应指出站点自检相关的问题
    assert result.error is not None
    # 断言说明中包含定位关键词
    assert "SiteName" in result.error or "自检" in result.error


def test_csrc_fetcher_passes_site_check_then_parses_api() -> None:
    """验证站点自检通过后，走接口取数并正确解析。"""
    # 导入证监会接口固定装置
    csrc_payload = load_fixture_json("csrc_search_list.json")

    # 路由：列表页返回带 meta 的 HTML，接口返回 JSON
    def router(url: str) -> FakeResponse:
        """按 URL 区分列表页与接口。"""
        # 接口请求
        if "/searchList/" in url:
            # 返回 JSON
            return FakeResponse(text=json.dumps(csrc_payload, ensure_ascii=False))
        # 列表页请求
        return FakeResponse(text=CSRC_META_HTML)

    # 构造 api 配置：字段映射照搬真实配置
    api = {
        "path": "/searchList/{channel_id}",
        "method": "GET",
        "channel_id": "test-channel",
        "params": {"_isJson": "true", "page": "{page}"},
        "items_path": "data.results",
        "field_map": {"title": "title", "url": "url", "date": "publishedTime"},
        "page_size": 3,
    }
    # 构造数据源
    source = _source_from({"fetcher": "csrc", "list_url": CSRC_LIST_URL, "api": api})
    # 执行抓取
    result = CsrcFetcher(source, session=FakeSession(router)).fetch()

    # 应成功
    assert result.status == "ok"
    # 装置中有 3 条
    assert len(result.docs) == 3
    # 日期来自 epoch 毫秒字段，应被解析出来
    assert all(doc.published_on is not None for doc in result.docs)
    # 相对链接应被补全为绝对链接
    assert result.docs[0].url.startswith("https://www.csrc.gov.cn/")


# ============================================================
# 基类行为 —— 跳过与降级
# ============================================================

def test_fetch_skips_disabled_source() -> None:
    """验证禁用的源被跳过，且不发出任何请求。"""
    # 构造禁用的源
    source = make_json_source(enabled=False)
    # 伪造会话，任何请求都会因路由未被调用而暴露
    session = FakeSession(_nfra_router)
    # 执行抓取
    result = JsonSearchListFetcher(source, session=session).fetch()

    # 状态为跳过
    assert result.status == "skipped"
    # 不应发出任何请求
    assert session.calls == []


def test_fetch_skips_manual_source() -> None:
    """验证人工录入源不执行网络抓取。"""
    # 构造 manual 类型的源
    source = make_json_source(fetcher="manual")
    # 伪造会话
    session = FakeSession(_nfra_router)
    # 执行抓取
    result = JsonSearchListFetcher(source, session=session).fetch()

    # 状态为跳过
    assert result.status == "skipped"
    # 不应发出任何请求
    assert session.calls == []


def test_fetch_reports_degraded_when_all_docs_are_navigation() -> None:
    """验证所有条目都被判为导航链接时降级，而不是「成功地」返回空结果。

    正常栏目页不可能全是导航链接。若出现这种情况，说明解析逻辑或
    页面结构出了问题，必须触发人工确认，不能静默地当作「无新政策」。
    """
    # 路由返回只有导航链接的页面
    def router(_url: str) -> FakeResponse:
        """返回只有导航链接的页面。"""
        # 返回 HTML
        return FakeResponse(text=NAV_ONLY_HTML)

    # 构造数据源
    source = _source_from(
        {
            "fetcher": "html_list",
            "list_url": "http://a.cn/x/y/index.html",
            "pagination": {"enabled": False, "max_pages": 1, "url_pattern": None},
            "selectors": {"item": "li", "title": "a", "link": "a", "date": "span"},
        }
    )
    # 执行抓取
    result = HtmlListFetcher(source, session=FakeSession(router)).fetch()

    # 应降级
    assert result.status == "degraded"
    # 剔除数量应被记录
    assert result.dropped_navigation == 2


# ============================================================
# 网信办抓取器 —— 以 2026-10 改版后的新页面结构为样本
# ============================================================
#
# 为什么这一组测试特别重要
# ------------------------
# 网信办源曾被误判为「网络不可达」而被禁用近一个月，实际原因只是 URL 失效
# （旧地址返回 404，而非超时）。恢复该源时页面结构已经改版，旧选择器
# （item="li"、date="span"）在新页面上会以两种方式静默失败：
#   1. item="li" 会把 13 条站点导航当成政策条目一并收进来
#   2. date="span" 在新页面匹配不到任何元素（该页没有 span），
#      日期全部丢失后，drop_probable_navigation 会把所有条目误判为导航而清空
# 二者都不会抛异常，只会让流水线报 ok 却拿到垃圾或空结果——正是本项目最警惕的
# 「静默污染」。下面三条测试分别锁死这两个陷阱以及真实的解析结果。

# 网信办「部门规章」栏目地址，与 data/sources.yaml 中 cac-department-rules 一致
CAC_DEPT_LIST_URL = "https://www.cac.gov.cn/wxzw/zcfg/bmgz/A09370303index_1.htm"

# 网信办列表页的选择器，与真实配置一致
CAC_SELECTORS = {
    # 定位到内容容器 #loadingInfoPage 内的 li。
    # 不用裸 "li"：该页 38 个 li 中有 13 个是站点导航，会被误收为政策条目。
    # 也不用 "li:has(div.times)"：那会让「日期解析失败」的政策被选择器直接排除，
    # 连进入清洗与台账的机会都没有——见 test_cac_fetcher_keeps_undated_items_that_are_not_navigation。
    "item": "#loadingInfoPage li",
    "title": "h5 a",               # 标题在 h5 内的链接上
    "link": "h5 a",                # 链接即标题链接
    "date": "div.times",           # 日期在 div.times，不是 span（该页没有任何 span）
}


def make_cac_source(**overrides: object):
    """构造使用 cac 抓取器的网信办数据源，默认选择器照搬真实配置。"""
    # 基线配置与 data/sources.yaml 中 cac-department-rules 保持一致
    defaults: dict = {
        "fetcher": "cac",                                   # 使用网信办专用抓取器
        "list_url": CAC_DEPT_LIST_URL,                      # 部门规章栏目地址
        "selectors": dict(CAC_SELECTORS),                   # 复制一份，避免测试间互相污染
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 交给共享装置构造
    return make_source(**defaults)


def _cac_router(url: str) -> FakeResponse:
    """把网信办列表页地址路由到本地固定装置。"""
    # 忽略具体 URL，一律返回部门规章栏目页的固定装置
    return FakeResponse(text=load_fixture_text("cac_bmgz_list.html"))


def test_cac_fetcher_parses_all_content_items_offline() -> None:
    """验证网信办列表页能被完整解析，且条目数与页面内容条目数一致。

    固定装置是 2026-10-05 从 https://www.cac.gov.cn/wxzw/zcfg/bmgz/A09370303index_1.htm
    原样保存的真实页面，其中含 <div class="times"> 的内容条目恰为 20 条。
    若解析结果少于此数，说明选择器把有效内容也排除掉了（漏检）。
    """
    # 构造数据源
    source = make_cac_source()
    # 执行抓取（固定装置路由，不触网）
    result = CacFetcher(source, session=FakeSession(_cac_router)).fetch()

    # 抓取应成功
    assert result.status == "ok"
    # 应解析出全部 20 条内容条目
    assert len(result.docs) == 20
    # 只应发出一次请求（该栏目已确认无分页）
    assert result.request_count == 1


def test_cac_fetcher_excludes_site_navigation() -> None:
    """验证网信办页面上的站点导航链接被排除，不会污染政策库。

    该页面共有 38 个 <li>，其中 13 个是站点导航（首页、时政要闻、网信政务、
    互动服务、热点专题，以及法律/行政法规/部门规章/司法解释/规范性文件/
    政策文件/政策解读七个子栏目入口）。这些导航标题很短、且部分含「法规」
    「政策」等字样，若用裸 "li" 选择器会被当成政策条目收录。

    这里断言「导航标题不出现在结果中」，是防守式断言：
    即使将来页面内容条目数变化，这条断言仍然有效。
    """
    # 构造数据源
    source = make_cac_source()
    # 执行抓取
    result = CacFetcher(source, session=FakeSession(_cac_router)).fetch()

    # 收集结果中的所有标题
    titles = {doc.title for doc in result.docs}
    # 这些标题全部是站点导航，绝不应出现在政策结果里
    for navigation_title in ("首 页", "时政要闻", "网信政务", "互动服务", "热点专题", "行政法规", "司法解释"):
        # 逐条断言不存在
        assert navigation_title not in titles, f"导航项「{navigation_title}」被误当作政策条目收录"


def test_cac_fetcher_extracts_dates_from_times_div() -> None:
    """验证日期从 div.times 正确提取，而不是全部丢失。

    这是本组测试的核心防线。旧的 date="span" 选择器在这个页面上匹配不到
    任何元素，会导致所有条目的 published_on 为 None；而 published_on 为 None
    正是 drop_probable_navigation 判定「疑似导航」的第一个条件。
    于是「日期选择器写错」这一处配置失误，会沿着
    「日期丢失 → 全被判为导航 → 结果清空」的链条放大为完全静默的失败。

    断言「超过一半条目有日期」而不是「全部有日期」，是因为：
    保留一点容错空间，避免个别条目日期格式异常时测试变得脆弱；
    但如果大面积丢日期，这条断言会立刻失败。
    """
    # 构造数据源
    source = make_cac_source()
    # 执行抓取
    result = CacFetcher(source, session=FakeSession(_cac_router)).fetch()

    # 统计有日期的条目数
    dated = [doc for doc in result.docs if doc.published_on is not None]
    # 绝大多数条目应带日期
    assert len(dated) > len(result.docs) / 2, "日期大面积丢失，检查 date 选择器是否与页面结构匹配"
    # 抽验一条已知条目：深度合成规定发布于 2022-12-11
    deepfake = [doc for doc in result.docs if "深度合成" in doc.title]
    # 该条应存在
    assert deepfake, "未解析出《互联网信息服务深度合成管理规定》"
    # 其日期应被正确解析
    assert deepfake[0].published_on is not None
    # 年份应为 2022
    assert deepfake[0].published_on.year == 2022


def test_cac_fetcher_keeps_undated_items_that_are_not_navigation() -> None:
    """验证「无日期但标题够长」的条目不会被误判为导航而丢弃。

    这是对 drop_probable_navigation 三条件判定的反向测试。
    导航链接的判定要求三个条件同时成立：无日期 + 链接不更深 + 标题短。
    本测试构造一条「无日期、但标题很长」的条目——
    它在真实页面上对应的是某个日期解析失败的正式政策。

    若该条目被丢弃，说明判定逻辑退化成「只看有无日期」，
    那会把所有日期解析失败的正式政策一并误杀，属于不可逆的数据损失。
    """
    # 构造一个只有两条内容的极简页面：一条有日期、一条无日期但标题很长
    html = (
        '<html><body><ul id="loadingInfoPage">'
        # 正常条目：含日期
        '<li><h5><a href=//www.cac.gov.cn/2026-04/10/c_x.htm target=_blank title="人工智能拟人化互动服务管理暂行办法">'
        '人工智能拟人化互动服务管理暂行办法</a></h5><div class="times">2026-04-10</div></li>'
        # 异常条目：没有 div.times（日期解析失败），但标题是完整的长句
        '<li><h5><a href=//www.cac.gov.cn/2026-03/01/c_y.htm target=_blank title="关于进一步加强生成式人工智能服务备案管理的若干意见">'
        '关于进一步加强生成式人工智能服务备案管理的若干意见</a></h5></li>'
        '</ul></body></html>'
    )

    # 路由恒定返回上述页面
    def router(_url: str) -> FakeResponse:
        """返回构造的极简页面。"""
        # 返回 HTML
        return FakeResponse(text=html)

    # 构造数据源
    source = make_cac_source()
    # 执行抓取
    result = CacFetcher(source, session=FakeSession(router)).fetch()

    # 两条都应保留：无日期不是丢弃的充分条件
    assert len(result.docs) == 2
    # 其中一条应有日期
    assert sum(1 for d in result.docs if d.published_on is not None) == 1
