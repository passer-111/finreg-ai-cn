"""核验取证层的离线测试 —— ``verify`` 模块的四个检查层。

为什么这组测试必须全离线
------------------------
证据卡的数据源是政府网站原文页面，但测试若依赖真实页面，
今天绿明天红（页面改版、限流、网络抖动），而代码一行没改。
因此全部页面内容用内联 HTML 固定装置注入，会话用 ``FakeSession``
按 URL 路由——与 ``test_fetchers_offline.py`` 同一模式。

覆盖矩阵（来自需求规格的四层）
------------------------------
层一（可达性）：200 通过 / 状态码非 200 红 / 重定向到站点首页红 /
  哈希变化黄 / 请求失败红。
层二（标题比对）：相似标题通过 / 标题不符红 / 无 <title> 标人工核。
层三（日期取证）：填写值有佐证通过 / 填了日期 0 命中红「疑似编造」/
  留空有命中黄「可补」/ 留空 0 命中通过 / 中文数字日期解析。
层四（义务核对）：定位失败红 / 重合度不足红 / 情态词漂移红 /
  全部一致通过 / 正文不可用标人工核。
另有贯穿规则：JS 空壳页（取不到正文）各层必须标 manual，
绝不允许当作通过——这是「明确失败优于静默错误」的落地。
"""

# 导入 date：构造固定的生效日期
from datetime import date
# 导入 json：验证证据卡可 JSON 序列化（界面与 --json 输出依赖这一点）
import json
# 导入 zipfile：测试内现场构造 DOCX（本质是 zip 包），保持全离线
import zipfile
# 导入 BytesIO：把构造的字节包装成文件对象
from io import BytesIO

# 导入 pytest 以使用装置与参数化
import pytest

# 导入被测的取证函数与状态常量
from finreg_ai.verify import (
    STATUS_ERROR,            # 红：证据冲突
    STATUS_MANUAL,           # 需人工核
    STATUS_OK,               # 绿：机器通过
    STATUS_WARNING,          # 黄：待判断
    AttachmentInfo,          # 附件取证结果（构造注入用）
    build_evidence_card,     # 单条出证（贯穿入口）
    check_dates,             # 层三
    check_obligations,       # 层四
    check_reachability,      # 层一
    check_title,             # 层二
    extract_attachment_links,  # 附件链接发现
    extract_content,         # 页面文本提取
    extract_docx_text,       # DOCX 文本提取
    extract_pdf_text,        # PDF 文本提取
    fetch_attachment,        # 附件下载与提取
    fetch_page,              # 页面抓取（FakeSession 注入点）
    find_effective_date_candidates,  # 候选日期提取
    load_drafts,             # 草稿加载
)
# 导入存取层原语：哈希计算（构造「记录哈希」基准用）
from finreg_ai.store import compute_hash, normalize_text
# 导入 requests 的异常类：伪造会话模拟网络层失败时使用
from requests.exceptions import ConnectTimeout

# 复用伪造会话与响应（conftest 中的离线基础设施）
from tests.conftest import FakeResponse, FakeSession
# 导入溯源、义务与核验方式类型：构造测试数据时直接实例化，语义更清晰
from finreg_ai.models import KeyObligation, SourceRef, VerifiedBy
# 复用政策工厂，避免把构造逻辑维护两遍
from tests.test_models import make_policy


# ============================================================
# 固定装置：HTML 页面
# ============================================================

# 一页「一切正常」的政策原文：标题与记录一致、含施行条款、含第十六条义务段落。
# 正文刻意写得足够长（超过 MIN_BODY_TEXT_LEN），否则会触发 JS 空壳判定。
GOOD_PAGE_HTML = """
<html><head><title>某测试政策_某测试机构</title></head>
<body>
<nav>首页 政策法规 统计数据 政务公开</nav>
<article>
<h1>某测试政策</h1>
<p>某测试政策已经 2026 年第 3 次局务会议审议通过，现予公布。（某机构发〔2026〕3号）</p>
<p>第一条 为规范人工智能在金融领域的应用，防范相关风险，制定本办法。</p>
<p>第二条 本办法适用于在中华人民共和国境内设立的金融机构。</p>
<p>第三条 金融机构应当建立人工智能应用的管理制度，明确牵头部门与职责分工。</p>
<p>第四条 金融机构应当建立模型风险分类分级管理机制，对高风险应用实施重点管控。</p>
<p>第五条 金融机构应当对外包合作机构实行名单制管理，防范供应链风险。</p>
<p>第六条 金融机构应当建立人工智能生成内容的显著标识机制，主动向金融消费者说明。</p>
<p>第七条 金融机构应当保留模型训练与推理的关键日志，保存期限不低于业务存续期。</p>
<p>第八条 金融机构应当建立人工智能伦理审查制度，避免算法歧视。</p>
<p>第九条 金融机构应当对高风险应用的关键环节建立人工监督与干预机制。</p>
<p>第十条 金融机构应当建立应急处置预案，明确紧急停用及模型退出条件。</p>
<p>第十一条 涉及资金交易、信贷审批等场景的应用视为高风险应用。</p>
<p>第十二条 金融机构应当对数据来源合法性进行审查，防范数据污染。</p>
<p>第十三条 金融机构应当对模型输出结果进行定期评估与验证。</p>
<p>第十四条 金融机构应当对内部人员开展人工智能合规培训。</p>
<p>第十五条 金融机构应当配合监管机构的现场检查与非现场监管。</p>
<p>第十六条 金融机构应当每年开展一次人工智能应用合规评估，并向监管机构报送评估报告。</p>
<p>第十七条 违反本办法的，依照有关法律法规给予处罚。</p>
<p>第十八条 本办法自 2026 年 7 月 1 日起施行。</p>
</article>
<footer>版权所有 某测试机构</footer>
</body></html>
"""

# 一篇 JS 渲染空壳页：只有骨架与脚本，正文要靠浏览器渲染。
# 核验台没有浏览器引擎，各检查层面对它必须标「需人工核」。
JS_SHELL_HTML = """
<html><head><title></title></head>
<body>
<div id="app"></div>
<script src="/static/js/vendor.js"></script>
<script src="/static/js/app.js"></script>
<script>window.__INITIAL_STATE__ = {};</script>
</body></html>
"""

# 一篇「伪全文」页：导航与页脚文字凑得很长（超过 MIN_BODY_TEXT_LEN），
# 但正文里既没有记录标题也没有发文字号——即 SPA 空壳页的导航部分。
# 这是实测踩过的坑：这种页面会骗过长度门槛，让「取不到证据」
# 被偷换成「全文检索不到」，进而把有依据的日期误判为「疑似编造」。
NAV_ONLY_PAGE_HTML = """
<html><head><title>某测试机构</title></head>
<body>
<nav>首页 机构概况 新闻发布 政策法规 统计数据 行政许可 行政处罚 互动交流 政务公开
在线办事 金融服务 消费者权益保护 专题专栏  English  手机版  网站地图</nav>
<div>您当前的位置：首页 &gt; 政策法规 &gt; 部门规章</div>
<aside>热门推荐：年度工作会议召开｜一季度监管数据发布｜公开征求意见公告｜
防范非法集资宣传月活动启动｜消费者风险提示第二期｜关于规范市场秩序的通知</aside>
<footer>版权所有 © 某测试机构 京ICP备00000000号-1 京公网安备 110000000000号
地址：北京市西城区某大街 1 号 邮编：100000 电话：010-00000000
网站标识码：bm00000000 建议使用 IE11 以上浏览器访问</footer>
</body></html>
"""


def _route_text(html: str, status: int = 200):
    """构造一个「无论请求什么都返回固定 HTML」的路由函数。"""
    # 返回路由函数
    def route(url: str) -> FakeResponse:
        # 返回伪造响应
        return FakeResponse(text=html, status_code=status)

    # 返回
    return route


def _policy_with_url(url: str = "https://example.gov.cn/policy/a.html"):
    """构造一条指向指定 URL 的测试政策（草稿形态）。"""
    # 复用工厂，覆盖溯源链接；标题与 GOOD_PAGE_HTML 的 <title> 一致
    return make_policy(title="某测试政策", source=SourceRef(
        url=url,                                    # 官方链接
        site="example.gov.cn",                      # 站点
        fetched_at="2026-10-03T12:00:00+08:00",     # 抓取时间
    ))


# ============================================================
# 页面抓取与内容提取
# ============================================================

def test_fetch_page_reports_http_status() -> None:
    """验证抓取结果如实记录状态码与最终 URL。"""
    # 构造会话：返回 200 页面
    session = FakeSession(_route_text(GOOD_PAGE_HTML))
    # 抓取
    page = fetch_page("https://example.gov.cn/policy/a.html", session)
    # 成功标志
    assert page.ok
    # 状态码
    assert page.status_code == 200
    # 未发生重定向
    assert not page.redirected_to_home
    # 有 HTML 内容
    assert page.html and "第十六条" in page.html


def test_fetch_page_flags_redirect_to_homepage() -> None:
    """验证「被重定向到站点首页」被识别——政府站删文的典型形态。"""
    # 构造路由：返回的响应带不同的 url（模拟跟随重定向后的落点）
    def route(url: str) -> FakeResponse:
        # 构造响应
        response = FakeResponse(text="<html>首页</html>", status_code=200)
        # 模拟 requests 跟随重定向后的最终 URL：站点首页
        response.url = "https://example.gov.cn/"  # type: ignore[attr-defined]
        # 返回
        return response

    # 构造会话
    session = FakeSession(route)
    # 抓取一个内容页地址
    page = fetch_page("https://example.gov.cn/policy/a.html", session)
    # 必须被识别为「重定向到首页」
    assert page.redirected_to_home


def test_fetch_page_does_not_flag_same_url_as_redirect() -> None:
    """反向测试：URL 未变化时不得误报重定向（夹逼上一条）。"""
    # 普通路由（响应不带 url 属性，退回请求 URL）
    session = FakeSession(_route_text(GOOD_PAGE_HTML))
    # 抓取
    page = fetch_page("https://example.gov.cn/policy/a.html", session)
    # 不得误报
    assert not page.redirected_to_home


def test_extract_content_strips_script_and_style() -> None:
    """验证脚本与样式文本被剔除，不污染正文检索。"""
    # 提取空壳页
    content = extract_content(JS_SHELL_HTML)
    # vendor.js 之类的脚本内容不应出现在正文里
    assert "vendor.js" not in content.body_text
    # 正文过短 → 不可用（JS 空壳判定）
    assert not content.usable


def test_extract_content_good_page_is_usable() -> None:
    """反向测试：正常政策页正文可用（夹逼上一条）。"""
    # 提取正常页
    content = extract_content(GOOD_PAGE_HTML)
    # 可用
    assert content.usable
    # 标题被提取
    assert content.title == "某测试政策_某测试机构"


# ============================================================
# 层一：可达性
# ============================================================

def test_reachability_ok_for_normal_page() -> None:
    """验证 200 且无异常重定向的页面通过可达性检查。"""
    # 构造记录与页面
    policy = _policy_with_url()
    # 抓取
    page = fetch_page(policy.source.url, FakeSession(_route_text(GOOD_PAGE_HTML)))
    # 提取内容
    content = extract_content(page.html or "")
    # 检查
    results = check_reachability(policy, page, content)
    # 单条结果
    assert len(results) == 1
    # 绿
    assert results[0].status == STATUS_OK


def test_reachability_error_on_http_error_status() -> None:
    """验证状态码非 200 时标红。"""
    # 构造记录
    policy = _policy_with_url()
    # 抓取一个 404
    page = fetch_page(policy.source.url, FakeSession(_route_text("不存在", status=404)))
    # 检查（内容无关紧要）
    results = check_reachability(policy, page, None)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论里如实写明状态码
    assert "404" in results[0].summary


def test_reachability_error_on_redirect_home() -> None:
    """验证被重定向到站点首页时标红。"""
    # 构造记录
    policy = _policy_with_url()

    # 构造重定向到首页的路由
    def route(url: str) -> FakeResponse:
        # 构造响应
        response = FakeResponse(text="<html>首页</html>", status_code=200)
        # 模拟落点到站点首页
        response.url = "https://example.gov.cn/"  # type: ignore[attr-defined]
        # 返回
        return response

    # 抓取
    page = fetch_page(policy.source.url, FakeSession(route))
    # 检查
    results = check_reachability(policy, page, None)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论说明删文形态
    assert "首页" in results[0].summary


def test_reachability_warning_on_hash_mismatch() -> None:
    """验证记录哈希与当前页面正文不符时标黄「页面已变」。"""
    # 构造记录：记录一个与当前页面不同的哈希
    policy = _policy_with_url()
    # 覆盖记录哈希为「另一个页面」的哈希
    policy.source.content_hash = compute_hash(normalize_text("另一份正文内容，完全不同的文本"))
    # 抓取当前页
    page = fetch_page(policy.source.url, FakeSession(_route_text(GOOD_PAGE_HTML)))
    # 提取内容
    content = extract_content(page.html or "")
    # 检查
    results = check_reachability(policy, page, content)
    # 黄
    assert results[0].status == STATUS_WARNING
    # 结论说明页面已变
    assert "哈希" in results[0].summary


def test_reachability_ok_when_hash_matches() -> None:
    """反向测试：哈希一致时不标黄（夹逼上一条）。"""
    # 先提取当前页内容，算出它的真实哈希作为记录基准
    content = extract_content(GOOD_PAGE_HTML)
    # 构造记录并写入正确哈希
    policy = _policy_with_url()
    # 写入匹配哈希
    policy.source.content_hash = compute_hash(content.body_text)
    # 抓取
    page = fetch_page(policy.source.url, FakeSession(_route_text(GOOD_PAGE_HTML)))
    # 检查
    results = check_reachability(policy, page, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_reachability_error_on_request_failure() -> None:
    """验证请求本身失败时标红且附失败原因。"""
    # 构造一个抛异常的路由
    def route(url: str) -> FakeResponse:
        # 模拟网络层失败（必须是 requests 的异常，fetch_page 只捕获这一类）
        raise ConnectTimeout("connection timed out")

    # 构造记录
    policy = _policy_with_url()
    # 抓取（fetch_page 应吞掉异常转成失败结果）
    page = fetch_page(policy.source.url, FakeSession(route))
    # 失败标志
    assert not page.ok
    # 检查
    results = check_reachability(policy, page, None)
    # 红
    assert results[0].status == STATUS_ERROR
    # 附失败原因
    assert "无法访问" in results[0].summary


# ============================================================
# 层二：标题比对
# ============================================================

def test_title_ok_when_page_title_matches() -> None:
    """验证页面 <title> 带站点后缀时仍与记录标题判为一致。"""
    # 构造记录
    policy = _policy_with_url()
    # 提取内容（<title> 为「某测试政策_某测试机构」）
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_title(policy, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_title_manual_when_page_is_another_article_without_doc_evidence() -> None:
    """验证页面是另一篇文章、且记录侧无文号可佐证时标「需人工核」。

    为什么不是红：单页取证无法区分「JS 空壳页」与「另一篇文件的全文页」
    ——两者都不含本记录的标题与文号。能证明「页面是全文」的唯一途径
    是文号命中（标题若命中，路径三已通过）。旧版在此直接指控
    「链接指向另一篇文章」，是取证失败偷换成证据冲突（与 P1 同类）。
    """
    # 构造记录（工厂默认无 doc_number）
    policy = _policy_with_url()
    # 一篇标题完全不同的页面：全局替换文件名为另一篇
    # （<title>、<h1>、首段三处一起换；只换 <title> 会命中
    #   「标题出现在正文」的通过路径——那不是误报，
    #   是这个 fixture 没造对「另一篇文章」）
    other_html = GOOD_PAGE_HTML.replace("某测试政策", "关于召开年度表彰大会的通知")
    # 提取内容
    content = extract_content(other_html)
    # 检查
    results = check_title(policy, content)
    # manual：取不到证据，不得指控
    assert results[0].status == STATUS_MANUAL
    # 结论写明需人工核
    assert "需人工核" in results[0].summary


def test_title_error_when_doc_number_proves_fulltext_but_title_differs() -> None:
    """反向夹逼：文号证明页面确为全文、标题却对不上时，红必须保留。

    这是标题层唯一能合法举红的情形——典型场景是记录标题用了
    立项名而页面是公布名（B 线 cba 草稿的真实情况），需要人改标题。
    """
    # 构造记录：标题与页面完全不同，但文号与页面一致
    policy = make_policy(
        title="另一个完全不同的管理办法",                 # 与页面标题对不上
        doc_number="某机构发〔2026〕3号",                 # 文号与页面一致
        source=_policy_with_url().source,                # 溯源
    )
    # 页面是全文（含该文号），但标题是「某测试政策」
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_title(policy, content)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论提示「记录标题与公布名不一致」的可能
    assert "公布名" in results[0].summary


def test_title_manual_when_no_title_tag() -> None:
    """验证页面没有 <title> 时标「需人工核」而非判为不一致。"""
    # 构造记录
    policy = _policy_with_url()
    # 无 <title> 的页面
    no_title_html = GOOD_PAGE_HTML.replace("<title>某测试政策_某测试机构</title>", "")
    # 提取内容
    content = extract_content(no_title_html)
    # 检查
    results = check_title(policy, content)
    # manual：取不到证据不得当作通过，也不得冤枉记录
    assert results[0].status == STATUS_MANUAL


def test_title_ok_when_wrapped_in_announcement() -> None:
    """P2 路径一：「关于印发《X》的通知」式标题经归一化后判通过。"""
    # 构造记录
    policy = _policy_with_url()
    # 印发通知式 <title>（政府站极常见形态）
    wrapped_html = GOOD_PAGE_HTML.replace(
        "<title>某测试政策_某测试机构</title>",
        "<title>某测试机构关于印发《某测试政策》的通知</title>",
    )
    # 提取内容
    content = extract_content(wrapped_html)
    # 检查
    results = check_title(policy, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_title_ok_when_doc_number_embedded() -> None:
    """P2 路径一：标题里嵌发文字号（〔2026〕3号）经剥除后判通过。"""
    # 构造记录
    policy = _policy_with_url()
    # 嵌文号的 <title>
    numbered_html = GOOD_PAGE_HTML.replace(
        "<title>某测试政策_某测试机构</title>",
        "<title>某测试政策（某机构发〔2026〕3号）_某测试机构</title>",
    )
    # 提取内容
    content = extract_content(numbered_html)
    # 检查
    results = check_title(policy, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_title_ok_when_title_is_site_name_but_body_matches() -> None:
    """P2 路径三：<title> 只是站名、但记录标题完整出现在正文时判通过，
    并在结论里如实说明命中位置。"""
    # 构造记录
    policy = _policy_with_url()
    # <title> 只剩站名（SPA 页常见），正文仍含完整标题（<h1>）
    site_title_html = GOOD_PAGE_HTML.replace(
        "<title>某测试政策_某测试机构</title>", "<title>某测试机构</title>"
    )
    # 提取内容
    content = extract_content(site_title_html)
    # 检查
    results = check_title(policy, content)
    # 绿
    assert results[0].status == STATUS_OK
    # 结论如实说明是正文命中，而非 <title> 命中
    assert "正文" in results[0].summary


# ============================================================
# 层三：日期与状态取证
# ============================================================

def test_date_candidates_extracted_from_effective_clause() -> None:
    """验证「自 X 年 X 月 X 日起施行」句式被解析为候选日期。"""
    # 提取
    candidates = find_effective_date_candidates("本办法自 2026 年 7 月 1 日起施行。")
    # 一个候选
    assert candidates == [date(2026, 7, 1)]


def test_date_candidates_support_chinese_numerals() -> None:
    """验证中文数字日期（二〇二一年十一月一日）同样被解析。"""
    # 提取（个保法施行条款的实际写法）
    candidates = find_effective_date_candidates("本法自二〇二一年十一月一日起施行。")
    # 解析正确
    assert candidates == [date(2021, 11, 1)]


def test_dates_ok_when_filled_value_is_corroborated() -> None:
    """验证填写的生效日期在原文有佐证时通过。"""
    # 构造记录：effective_from 与 GOOD_PAGE_HTML 的施行条款一致
    policy = _policy_with_url()
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_dates(policy, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_dates_error_when_filled_but_zero_hits() -> None:
    """验证填了日期而全文 0 命中时标红「疑似编造日期」。

    这是本层最重要的规则：它拦的是「自动化整理倾向把字段填满」。
    """
    # 构造记录：填了日期
    policy = _policy_with_url()
    # 一篇没有任何施行/生效/废止表述的页面
    no_date_html = GOOD_PAGE_HTML.replace("第十八条 本办法自 2026 年 7 月 1 日起施行。", "第十八条 本办法由某测试机构负责解释。")
    # 提取内容
    content = extract_content(no_date_html)
    # 检查
    results = check_dates(policy, content)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论指明疑似编造
    assert "疑似编造" in results[0].summary
    # 提供「清空日期」选项
    assert any(o.action.get("kind") == "set-field" for o in results[0].options)


def test_dates_warning_when_blank_but_candidate_exists() -> None:
    """验证留空而有命中时标黄「可补」并给出采纳选项。"""
    # 构造记录：生效日期留空
    policy = make_policy(title="某测试政策", effective_from=None, source=_policy_with_url().source)
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_dates(policy, content)
    # 黄
    assert results[0].status == STATUS_WARNING
    # 结论说明可补
    assert "可补" in results[0].summary
    # 提供采纳候选日期的选项
    assert any(o.key == "adopt:2026-07-01" for o in results[0].options)


def test_dates_ok_when_blank_and_zero_hits() -> None:
    """反向测试：留空且 0 命中时通过——留空与原文一致（夹逼上面两条）。"""
    # 构造记录：留空
    policy = make_policy(title="某测试政策", effective_from=None, source=_policy_with_url().source)
    # 无日期表述的页面
    no_date_html = GOOD_PAGE_HTML.replace("第十八条 本办法自 2026 年 7 月 1 日起施行。", "第十八条 本办法由某测试机构负责解释。")
    # 提取内容
    content = extract_content(no_date_html)
    # 检查
    results = check_dates(policy, content)
    # 绿
    assert results[0].status == STATUS_OK


def test_dates_manual_when_body_unusable() -> None:
    """验证正文不可用（JS 空壳）时标「需人工核」。"""
    # 构造记录
    policy = _policy_with_url()
    # 空壳页内容
    content = extract_content(JS_SHELL_HTML)
    # 检查
    results = check_dates(policy, content)
    # manual
    assert results[0].status == STATUS_MANUAL
    # 结论写明需人工核
    assert "需人工核" in results[0].summary


def test_dates_manual_not_fabrication_when_page_is_not_fulltext() -> None:
    """P1 修复核心：填了日期、0 命中、但页面不是全文页时，
    必须降级为「需人工核」而不是指控「疑似编造日期」。

    「取证失败」与「证据冲突」是两种东西——实测里 nfra 的 JS 空壳页
    曾让一份有原文依据的日期被误标为疑似编造。
    """
    # 构造记录：填了日期
    policy = _policy_with_url()
    # 伪全文页（导航文字凑够长度门槛，但无全文特征）
    content = extract_content(NAV_ONLY_PAGE_HTML)
    # 前提：长度门槛确实被通过了（否则走不到本分支，测试空转）
    assert content.usable
    # 检查
    results = check_dates(policy, content)
    # 必须是 manual 而不是 error
    assert results[0].status == STATUS_MANUAL
    # 绝不允许出现「疑似编造」的指控
    assert "疑似编造" not in results[0].summary


def test_dates_still_flags_fabrication_on_true_fulltext() -> None:
    """反向夹逼：页面确为全文（含记录标题）、填了日期、0 命中时，
    「疑似编造日期」的红旗必须照常举起——不能因为修 P1 而拆掉它。"""
    # 构造记录：填了日期
    policy = _policy_with_url()
    # 真全文页但无日期表述（标题在 <h1> 里，全文特征成立）
    no_date_html = GOOD_PAGE_HTML.replace("第十八条 本办法自 2026 年 7 月 1 日起施行。", "第十八条 本办法由某测试机构负责解释。")
    # 提取内容
    content = extract_content(no_date_html)
    # 检查
    results = check_dates(policy, content)
    # 仍然是红
    assert results[0].status == STATUS_ERROR
    # 仍然指控疑似编造
    assert "疑似编造" in results[0].summary


def test_dates_manual_when_blank_and_page_not_fulltext() -> None:
    """P1 修复另一半：留空、0 命中、页面非全文时不得给绿。

    旧逻辑下这是绿「留空与原文一致」——空壳页自动放行，
    比误红更糟，是静默通过。
    """
    # 构造记录：留空
    policy = make_policy(title="某测试政策", effective_from=None, source=_policy_with_url().source)
    # 伪全文页
    content = extract_content(NAV_ONLY_PAGE_HTML)
    # 检查
    results = check_dates(policy, content)
    # manual，不是绿
    assert results[0].status == STATUS_MANUAL


def test_dates_still_ok_when_blank_and_true_fulltext_zero_hits() -> None:
    """反向夹逼：页面确为全文、留空、0 命中时，绿必须保留。"""
    # 构造记录：留空
    policy = make_policy(title="某测试政策", effective_from=None, source=_policy_with_url().source)
    # 真全文页无日期表述
    no_date_html = GOOD_PAGE_HTML.replace("第十八条 本办法自 2026 年 7 月 1 日起施行。", "第十八条 本办法由某测试机构负责解释。")
    # 提取内容
    content = extract_content(no_date_html)
    # 检查
    results = check_dates(policy, content)
    # 仍然是绿
    assert results[0].status == STATUS_OK


# ============================================================
# 层四：义务清单核对
# ============================================================

def _policy_with_obligation(summary: str, clause: str = "十六", obligation_type: str | None = None):
    """构造一条含单条义务的测试政策。"""
    # 复用工厂
    policy = _policy_with_url()
    # 覆盖义务清单（KeyObligation 的字段与 schema 一致）
    policy.key_obligations = [KeyObligation(clause=clause, summary=summary, obligation_type=obligation_type)]
    # 返回
    return policy


def test_obligations_ok_when_clause_and_summary_match() -> None:
    """验证条款定位成功且概括与原段一致时通过。"""
    # 概括与第十六条原文高度一致
    policy = _policy_with_obligation("金融机构应当每年开展一次人工智能应用合规评估并向监管机构报送评估报告")
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_obligations(policy, content)
    # 单条结果
    assert len(results) == 1
    # 绿
    assert results[0].status == STATUS_OK


def test_obligations_error_when_clause_not_found() -> None:
    """验证条款号在原文中定位失败时标红。"""
    # 构造含第九十九条义务的记录（原文没有）
    policy = _policy_with_obligation("某条不存在的义务", clause="九十九")
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_obligations(policy, content)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论说明定位失败
    assert "定位" in results[0].summary
    # 提供「删除该条目」选项
    assert any(o.action.get("kind") == "drop-obligation" for o in results[0].options)


def test_obligations_error_when_summary_unrelated() -> None:
    """验证概括与原段风马牛不相及时标红（重合度不足）。"""
    # 概括与第十六条毫无关系
    policy = _policy_with_obligation("应当建立完善的食堂餐饮卫生管理制度与年度体检流程")
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_obligations(policy, content)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论说明重合度不足
    assert "重合度" in results[0].summary


def test_obligations_error_on_modal_word_drift() -> None:
    """验证原文为鼓励性表述却标 mandatory 时标红（情态词漂移）。"""
    # 构造一个含「鼓励」条款的页面
    encourage_html = GOOD_PAGE_HTML.replace(
        "第十六条 金融机构应当每年开展一次人工智能应用合规评估，并向监管机构报送评估报告。",
        "第十六条 鼓励金融机构开展人工智能应用合规评估，分享评估经验。",
    )
    # 概括与改后的段落一致，但类型标为 mandatory
    policy = _policy_with_obligation("鼓励金融机构开展人工智能应用合规评估并分享评估经验", obligation_type="mandatory")
    # 提取内容
    content = extract_content(encourage_html)
    # 检查
    results = check_obligations(policy, content)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论说明情态词漂移
    assert "情态" in results[0].summary
    # 提供「改为 encouraged」选项
    assert any(o.action.get("value") == "encouraged" for o in results[0].options)


def test_obligations_ok_when_no_obligations() -> None:
    """验证义务清单为空（草稿常态）时通过并如实说明。"""
    # 构造记录（工厂默认无义务）
    policy = _policy_with_url()
    # 提取内容
    content = extract_content(GOOD_PAGE_HTML)
    # 检查
    results = check_obligations(policy, content)
    # 绿
    assert results[0].status == STATUS_OK
    # 如实说明无条目
    assert "无" in results[0].summary or "暂无" in results[0].summary


def test_obligations_manual_when_body_unusable() -> None:
    """验证正文不可用时每条义务标「需人工核」而非当作通过。"""
    # 构造含义务的记录
    policy = _policy_with_obligation("金融机构应当每年开展一次人工智能应用合规评估并向监管机构报送评估报告")
    # 空壳页内容
    content = extract_content(JS_SHELL_HTML)
    # 检查
    results = check_obligations(policy, content)
    # manual
    assert results[0].status == STATUS_MANUAL


def test_obligations_locate_fail_on_nav_page_is_manual_not_error() -> None:
    """验证导航页上定位失败标「需人工核」而非红（P3 校准的铁律5分流）。

    这是 test_obligations_error_when_clause_not_found 的配对反向测试：
    同一个「第九十九条」，在全文页上是红（证据冲突），在导航页上
    必须是 manual（取证失败）——P3 实测 25 条义务曾因缺这道分流
    在壳页/机构首页上被误报「条款号填写有误」。
    """
    # 构造含第九十九条义务的记录（导航页里没有，全文页里也没有）
    policy = _policy_with_obligation("某条不存在的义务", clause="九十九")
    # 导航页内容（≥200 字符、正文标记可用，但不含本记录任何特征）
    content = extract_content(NAV_ONLY_PAGE_HTML)
    # 前置断言：fixture 必须被判定为「可用但非全文」，否则本测试空转
    assert content.usable
    # 检查
    results = check_obligations(policy, content)
    # manual 而非 error
    assert results[0].status == STATUS_MANUAL
    # 结论说明是取证失败而非条款号有误
    assert "无法自动取证" in results[0].summary


def test_obligations_subsection_clause_locates() -> None:
    """验证「（十五）」子条编号能定位（金规文件常见形态，P3 实测 14 条义务全是这种）。"""
    # 构造子条编号页面：标题 + （一）至（十六）的分项列表
    # （正文须超过 200 字符的可用门槛，否则在正文可用性那一关就被拦下，
    # 走不到条款定位——第一条 fixture 就是踩的这个坑）
    subsection_html = (
        "<html><head><title>某测试政策_某测试机构</title></head><body>"
        "<h1>某测试政策</h1>"
        "<p>第一条 为规范人工智能在金融机构的开发与应用，防范化解相关风险，"
        "保护金融消费者合法权益，根据有关法律法规，现将有关事项通知如下。</p>"
        "<p>第二条 各机构应当充分认识人工智能应用的重要意义，坚持安全与发展并重，"
        "建立健全内部管理制度，明确责任分工，确保各项要求落实到位。</p>"
        "<p>二、机构管理要求</p>"
        "<p>（一）应当建立人工智能治理架构，明确董事会职责与跨部门协同机制。</p>"
        "<p>（十五）应当按业务场景重要性、应用规模、对客影响度开展风险识别与分类分级管理。</p>"
        "<p>（十六）高风险应用须经本机构人工智能治理委员会审议后方可上线运行。</p>"
        "</body></html>"
    )
    # 概括与（十五）原段一致
    policy = _policy_with_obligation(
        "应当按业务场景重要性、应用规模、对客影响度开展风险识别与分类分级管理",
        clause="（十五）",
    )
    # 提取内容
    content = extract_content(subsection_html)
    # 检查
    results = check_obligations(policy, content)
    # 绿（子条定位成功 + 概括一致）
    assert results[0].status == STATUS_OK


def test_obligations_literal_fallback_when_clause_has_no_number() -> None:
    """验证无数字语义的 clause 走字面匹配（此路径曾被注释承诺但从未实现）。"""
    # 构造含「附件」段落的页面（正文同样须超过 200 字符的可用门槛）
    appendix_html = (
        "<html><head><title>某测试政策_某测试机构</title></head><body>"
        "<h1>某测试政策</h1>"
        "<p>第一条 为规范人工智能科技活动伦理审查，保障科技活动安全可靠、"
        "可控可信，促进人工智能健康发展，根据有关法律法规，制定本办法。</p>"
        "<p>第二条 开展人工智能科技活动的单位应当履行伦理审查主体责任，"
        "建立健全审查制度，配备必要的人员与条件，保证审查工作独立、客观、公正。</p>"
        "<p>附件 高风险活动清单：一、对人类主观行为、心理情绪和生命健康"
        "具有较强影响的人机融合系统的研发；二、具有舆论社会动员能力的算法模型开发。</p>"
        "</body></html>"
    )
    # clause 为「附件」——没有数字语义，只能靠字面匹配定位
    policy = _policy_with_obligation(
        "高风险活动清单：对人类主观行为、心理情绪和生命健康具有较强影响的人机融合系统研发",
        clause="附件",
    )
    # 提取内容
    content = extract_content(appendix_html)
    # 检查
    results = check_obligations(policy, content)
    # 绿（字面定位成功 + 概括一致）
    assert results[0].status == STATUS_OK


# ============================================================
# 贯穿：完整证据卡
# ============================================================

def test_build_evidence_card_covers_all_four_layers() -> None:
    """验证完整出证覆盖四个检查层，且结果可 JSON 序列化。"""
    # 构造记录
    policy = _policy_with_url()
    # 出证
    card = build_evidence_card(policy, FakeSession(_route_text(GOOD_PAGE_HTML)))
    # 覆盖四个层
    layers = {c.layer for c in card.checks}
    # 四层齐全
    assert {"可达性", "标题比对", "日期与状态", "义务核对"} <= layers
    # 可 JSON 序列化（界面与 --json 输出依赖这一点）
    json.dumps(card.to_dict(), ensure_ascii=False)


def test_build_evidence_card_js_shell_marks_manual_everywhere() -> None:
    """贯穿规则：JS 空壳页上，依赖正文的各层必须标 manual，不得全绿。

    一张全绿的证据卡与一张「取不到证据」的卡必须一眼可辨。
    若这层失守，核验台会把「什么都没核」显示成「机器已通过」——
    正是本项目最忌讳的静默通过。
    """
    # 构造含义务的记录（让义务层也有东西可核）
    policy = _policy_with_obligation("金融机构应当每年开展一次人工智能应用合规评估并向监管机构报送评估报告")
    # 对空壳页出证
    card = build_evidence_card(policy, FakeSession(_route_text(JS_SHELL_HTML)))
    # 标题层：无 <title> 文本 → manual
    title_check = next(c for c in card.checks if c.layer == "标题比对")
    # 必须 manual
    assert title_check.status == STATUS_MANUAL
    # 日期层：正文不可用 → manual
    date_check = next(c for c in card.checks if c.layer == "日期与状态")
    # 必须 manual
    assert date_check.status == STATUS_MANUAL
    # 义务层：正文不可用 → manual
    obligation_checks = [c for c in card.checks if c.layer == "义务核对"]
    # 必须全部 manual
    assert all(c.status == STATUS_MANUAL for c in obligation_checks)
    # 待判断项非空（界面必须要求人表态）
    assert card.pending_checks()


# ============================================================
# 草稿加载：警告过滤
# ============================================================

def test_load_drafts_filters_definitional_warnings(tmp_path) -> None:
    """验证草稿加载丢弃「verified_by=automated」这类定义性警告，保留错误。

    草稿的定义就是「未经人工核验的候选记录」，语义校验对每条草稿
    必然产出该警告——11 条草稿每次出证打印 12 行恒真警告，
    真正的加载错误会被淹没（告警一多，人就习惯性忽略）。
    """
    # 导入 YAML 序列化与字典化工具（构造临时草稿文件）
    import yaml

    # 导入政策字典化函数
    from finreg_ai.models import policy_to_dict

    # 写一条合法草稿（verified_by 工厂默认 human，改为 automated 模拟草稿形态）。
    # domain 与 issuer_code 必须覆盖：工厂默认值 "banking" 不在 schema
    # 受控词表里（schema 用中文领域名），load_policy_file 会先跑
    # JSON Schema 校验，不覆盖的话草稿根本过不了结构关——
    # 这与 test_diff / test_site 的既有做法一致。
    draft = make_policy(
        title="某测试政策",                       # 标题
        verified_by=VerifiedBy.AUTOMATED,         # 草稿形态：机器整理未经人工核验
        domain=["银行", "人工智能"],              # 受控词表内的领域名
        issuer_code="nfra",                       # 受控词表内的机构代码
    )
    # 序列化写入临时目录
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(policy_to_dict(draft), allow_unicode=True), encoding="utf-8")
    # 写一份损坏的 YAML（顶层是列表而非映射）
    (tmp_path / "broken.yaml").write_text("- 这不是政策记录\n", encoding="utf-8")
    # 加载
    drafts, errors = load_drafts(tmp_path)
    # 合法草稿被加载
    assert "test-2026-demo" in drafts
    # 定义性警告被丢弃，但结构性错误被保留
    assert errors
    # 保留的是 broken.yaml 的结构错误
    assert any("broken.yaml" in e for e in errors)
    # 恒真的 automated 警告不在其中
    assert not any("尚未经人工核验" in e for e in errors)


def test_load_drafts_reports_missing_directory(tmp_path) -> None:
    """反向测试：草稿目录不存在时明确报错，而非静默返回空（夹逼上一条）。"""
    # 加载一个不存在的目录
    drafts, errors = load_drafts(tmp_path / "no-such-dir")
    # 空结果
    assert drafts == {}
    # 明确报错
    assert any("不存在" in e for e in errors)


# ============================================================
# 附件取证（公告 + 附件型发布）
# ============================================================
#
# 背景：监管机构有一类发布形态是「公告页 + 附件 PDF/DOCX」——
# 公告页只有几百字导语，实体条款在附件里。义务核对层必须能把
# 附件文本作为第二检索空间，且附件提取失败时标「需人工核」
# 而不是在公告页上找不到就标红（铁律 5 的附件形态）。

# 附件型公告页固定装置：标题在公告里（这是铁律 5 的陷阱形态——
# 公告含标题会让 _page_contains_document 误判为全文），
# 但正文只有导语，条款在附件里
ANNOUNCEMENT_PAGE_HTML = """
<html><head><title>关于发布《某测试政策》的公告_某测试机构</title></head><body>
<header>某测试机构 导航 首页 政策法规 通知公告 政务公开 互动交流 办事服务
热点专题 信用信息 统计信息 人员招聘 联系我们 网站地图 english 搜索</header>
<main>
<h1>关于发布《某测试政策》的公告</h1>
<p>根据有关规定，协会组织制定了《某测试政策》，经审议通过，现予发布，自公布之日起实施。特此公告。</p>
<p>附件：<a href="/files/zc/2026/test-policy.docx">某测试政策</a></p>
<p>某测试机构 2026年4月3日</p>
</main>
<footer>版权所有 某测试机构 京ICP备00000000号 京公网安备 110000000000号
建议使用 1024*768 以上分辨率浏览本站 流量统计 网站声明 联系我们</footer>
</body></html>
"""


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    """在内存中构造一个最小 DOCX（zip + word/document.xml），返回字节。

    测试必须全离线且不得依赖二进制 fixture 文件——现场构造是
    唯一的干净方式（DOCX 本质是 zip，标准库足够）。
    """
    # 段落拼成 w:p 结构
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    # 最小 document.xml
    document = f'<?xml version="1.0"?><w:document><w:body>{body}</w:body></w:document>'
    # 内存 zip
    buffer = BytesIO()
    # 写入
    with zipfile.ZipFile(buffer, "w") as archive:
        # 主文档
        archive.writestr("word/document.xml", document)
    # 返回字节
    return buffer.getvalue()


def test_extract_attachment_links_finds_and_resolves() -> None:
    """验证附件链接发现：识别扩展名、解析相对路径、去重、忽略非附件。"""
    # 页面里：一个相对 docx（正文与下载区各链一次）、一个 pdf、一个普通链接
    page = (
        '<a href="/files/a.docx">政策正文</a>'
        '<a href="/files/a.docx">附件下载：政策正文</a>'
        '<a href="https://static.example.gov.cn/b.pdf?ts=123">解读材料</a>'
        '<a href="/about.html">关于我们</a>'
    )
    # 提取
    links = extract_attachment_links(page, "https://example.gov.cn/notice/2026/c_1.htm")
    # 两个附件（docx 去重后一个 + pdf 一个）
    assert links == [
        ("https://example.gov.cn/files/a.docx", "docx"),
        ("https://static.example.gov.cn/b.pdf?ts=123", "pdf"),
    ]


def test_extract_docx_text_roundtrip() -> None:
    """验证 DOCX 提取：现场构造的 DOCX 能取回中文段落且段落边界保留。"""
    # 构造
    payload = _make_docx_bytes(["第十六条 金融机构应当开展合规评估。", "第十七条 不得滥用数据。"])
    # 提取
    text = extract_docx_text(payload)
    # 非空
    assert text is not None
    # 内容完整
    assert "第十六条" in text and "合规评估" in text
    # 段落边界保留为换行（条款段落提取依赖它）
    assert "\n" in text
    # 损坏输入明确返回 None（不抛异常、不静默返回空串）
    assert extract_docx_text(b"not a zip at all") is None


def _make_pdf_bytes(text: str) -> bytes:
    """构造一个带完整 xref 表的最小合法 PDF（单页、WinAnsi 文本）。

    pypdf 6.x 强制要求 startxref——不再像老版本那样容错重建，
    因此 xref 偏移量必须逐字节算对。只用 ASCII：中文文本流需要
    CID 字体表，那属于 pypdf 自己的测试范围，不是本项目的。
    """
    # 文本流（Tj 显示文本）
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    # 五个间接对象
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",                                       # 1 目录
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",                                 # 2 页树
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"                      # 3 页面
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode("ascii") + b">>\nstream\n"      # 4 内容流
        + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",                     # 5 字体
    ]
    # 逐对象拼接并记录偏移
    body = b"%PDF-1.4\n"
    # 偏移表
    offsets: list[int] = []
    # 逐个写入
    for number, obj in enumerate(objects, start=1):
        # 记录本对象起始偏移
        offsets.append(len(body))
        # 拼接对象
        body += f"{number} 0 obj".encode("ascii") + obj + b"\nendobj\n"
    # xref 表起始偏移
    xref_start = len(body)
    # 拼 xref 表（含第 0 号空闲对象）
    xref = b"xref\n0 " + str(len(objects) + 1).encode("ascii") + b"\n"
    # 空闲对象行
    xref += b"0000000000 65535 f \n"
    # 各对象行（10 位偏移 + 代际号 + n）
    for offset in offsets:
        # 追加一行
        xref += f"{offset:010d} 00000 n \n".encode("ascii")
    # trailer 与 startxref
    trailer = (b"trailer<</Size " + str(len(objects) + 1).encode("ascii")
               + b"/Root 1 0 R>>\nstartxref\n"
               + str(xref_start).encode("ascii") + b"\n%%EOF\n")
    # 返回完整字节
    return body + xref + trailer


def test_extract_pdf_text_smoke_with_real_pypdf() -> None:
    """冒烟：真实 pypdf 能解析一个最小合法 PDF（验证依赖本身可用）。"""
    # 提取
    text = extract_pdf_text(_make_pdf_bytes("Hello PDF"))
    # 非空且含文本
    assert text is not None and "Hello PDF" in text
    # 垃圾输入明确返回 None
    assert extract_pdf_text(b"%%PDF broken") is None


def test_fetch_attachment_downloads_and_extracts_docx() -> None:
    """验证附件下载提取：状态、文本长度、失败路径都如实记录。"""
    # 构造 DOCX 字节
    payload = _make_docx_bytes(["第十六条 金融机构应当开展合规评估。"])
    # 伪造会话：附件地址返回字节
    session = FakeSession(lambda url: FakeResponse(content=payload))
    # 下载提取
    info = fetch_attachment("https://example.gov.cn/files/a.docx", "docx", session)
    # 成功
    assert info.status == "extracted"
    # 文本在内存里
    assert info.text is not None and "第十六条" in info.text
    # 长度记录
    assert info.text_len == len(info.text)
    # 失败路径：404
    session_404 = FakeSession(lambda url: FakeResponse(text="nf", status_code=404))
    # 下载
    info_404 = fetch_attachment("https://example.gov.cn/files/a.docx", "docx", session_404)
    # 明确失败
    assert info_404.status == "failed" and "404" in (info_404.error or "")
    # 不支持的格式：老 .doc 不下载直接标 unsupported
    info_doc = fetch_attachment("https://example.gov.cn/files/a.doc", "doc", session)
    # 不支持
    assert info_doc.status == "unsupported"


def test_obligations_locate_in_attachment_when_page_is_announcement() -> None:
    """核心场景：公告页只有导语，条款在附件里——义务核对搜附件全文。"""
    # 义务概括与附件中的条款一致
    policy = _policy_with_obligation("金融机构应当每年开展一次人工智能应用合规评估并向监管机构报送评估报告")
    # 公告页内容（正文可用但无条款）
    content = extract_content(ANNOUNCEMENT_PAGE_HTML)
    # 附件已提取文本（含第十六条）
    attachments = [AttachmentInfo(
        url="https://example.gov.cn/files/test-policy.docx", kind="docx", status="extracted",
        text_len=80, text="第十六条 金融机构应当每年开展一次人工智能应用合规评估，并向监管机构报送评估报告。",
    )]
    # 检查
    results = check_obligations(policy, content, attachments=attachments)
    # 绿
    assert results[0].status == STATUS_OK
    # 结论注明出自附件（人要知道机器核的是哪份文本）
    assert "附件" in results[0].summary


def test_obligations_error_when_clause_absent_from_both_page_and_attachment() -> None:
    """验证附件全文已检索仍未找到时标红——附件就是实体全文，找不到是证据冲突。"""
    # 构造含第九十九条义务的记录（公告页与附件都没有）
    policy = _policy_with_obligation("某条不存在的义务", clause="九十九")
    # 公告页内容
    content = extract_content(ANNOUNCEMENT_PAGE_HTML)
    # 附件已提取（内容是别的条款）
    attachments = [AttachmentInfo(
        url="https://example.gov.cn/files/test-policy.docx", kind="docx", status="extracted",
        text_len=40, text="第十六条 金融机构应当每年开展一次合规评估。",
    )]
    # 检查
    results = check_obligations(policy, content, attachments=attachments)
    # 红
    assert results[0].status == STATUS_ERROR
    # 结论说明两边都搜过
    assert "附件" in results[0].summary


def test_obligations_manual_when_attachment_extraction_failed_on_announcement() -> None:
    """铁律 5 的附件形态：公告含标题（看似全文）但条款在提取失败的附件里——必须 manual 而非红。

    这是本组测试的灵魂：公告型页面的标题命中会让
    ``_page_contains_document`` 判 True，若没有「有附件但提取失败」
    这道分流，义务核对会在只搜了导语的情况下报「条款号填写有误」——
    取证失败被误报成证据冲突。
    """
    # 构造含第九十九条义务的记录
    policy = _policy_with_obligation("某条义务", clause="九十九")
    # 公告页内容（含标题，正文可用）
    content = extract_content(ANNOUNCEMENT_PAGE_HTML)
    # 附件提取失败（扫描件/损坏）
    attachments = [AttachmentInfo(
        url="https://example.gov.cn/files/test-policy.docx", kind="docx", status="failed",
        error="附件文本提取失败（可能为扫描件或文件损坏）",
    )]
    # 检查
    results = check_obligations(policy, content, attachments=attachments)
    # manual 而非 error
    assert results[0].status == STATUS_MANUAL
    # 结论说明实体内容可能在附件
    assert "附件" in results[0].summary


def test_build_evidence_card_fetches_attachment_end_to_end() -> None:
    """端到端：公告页 + DOCX 附件，证据卡的义务核对走附件文本并记录附件状态。"""
    # 义务概括与附件条款一致
    policy = _policy_with_obligation("金融机构应当每年开展一次人工智能应用合规评估并向监管机构报送评估报告")
    # 附件字节
    payload = _make_docx_bytes([
        "第十六条 金融机构应当每年开展一次人工智能应用合规评估，并向监管机构报送评估报告。",
    ])
    # 路由：公告页返回 HTML，附件返回 DOCX 字节
    session = FakeSession(lambda url: (
        FakeResponse(content=payload) if url.endswith(".docx")
        else FakeResponse(text=ANNOUNCEMENT_PAGE_HTML, status_code=200)
    ))
    # 出证
    card = build_evidence_card(policy, session)
    # 附件被记录且提取成功
    assert len(card.attachments) == 1
    # 成功
    assert card.attachments[0].status == "extracted"
    # 义务核对绿且出自附件
    obligation_results = [c for c in card.checks if c.layer == "义务核对"]
    # 绿
    assert obligation_results[0].status == STATUS_OK
    # 出自附件
    assert "附件" in obligation_results[0].summary
    # 证据卡可 JSON 序列化（P5 落盘路径依赖这一点，附件文本不得混入）
    json.dumps(card.to_dict(), ensure_ascii=False)


if __name__ == "__main__":
    # 允许直接运行本文件做快速自检
    raise SystemExit(pytest.main([__file__, "-q"]))
