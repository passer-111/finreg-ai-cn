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

# 导入待测抓取器与工具
from finreg_ai.fetchers.csrc import CsrcFetcher, extract_meta
from finreg_ai.fetchers.html_list import HtmlListFetcher
from finreg_ai.fetchers.json_search_list import JsonSearchListFetcher
from finreg_ai.fetchers.pbc import PbcFetcher

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
