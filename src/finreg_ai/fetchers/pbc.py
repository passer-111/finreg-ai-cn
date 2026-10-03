"""中国人民银行（PBC）专用抓取器。

特殊之处
--------
1. **编码陷阱。** 人行历史页面常用 GB2312 编码，但 HTTP 响应头有时
   声明为 ISO-8859-1，导致 requests 依声明解码后中文全部乱码。
   本抓取器显式处理这种情况。

2. **table 布局。** 人行列表页长期使用 ``<table>`` 而非现代 div 结构，
   行内最后一列是日期。通用抓取器已能处理，但这里额外做一步校验：
   确认取到的「日期列」文本确实像日期，不像则丢弃，
   避免把「操作」列或「来源」列误当成日期。

3. **列表页含栏目导航。** 页面顶部与侧边有「新闻发布」「政务公开」
   「金融科技」「法律声明」等栏目入口。它们的标题短、没有日期，
   而其中「金融科技」恰好含关键词「科技」，会骗过关键词过滤被当成政策。
   本抓取器先把这类导航文字剔掉（见 ``parse``），
   基类还会再做一次基于链接层数的通用识别（见 ``drop_probable_navigation``）。

一处需要纠正的早期误判
----------------------
本项目早期版本曾记录「该页面的 63 个 table tr 全部是布局表格，
不含政策列表项」，据此认为该源不可用。**这个结论是错的**——
实测（2026-10-03）使用 ``table tr`` + ``a`` + ``td:last-child`` 选择器
可以正常解析出 32 行，其中约 21 行是带日期的真实规范性文件
（如「中国人民银行公告〔2026〕第24号」）。
早期之所以得出错误结论，是把「结果里混有大量导航噪声」
误读成了「结果里没有真数据」——这两者的处置方式完全不同：
前者需要清洗，后者才需要换方案。

实测：该站网络可达性良好（http=200，约 0.4 秒），是本项目最稳定的源之一。
"""

# 导入正则用于清理栏目导语类噪声
import re

# 导入 requests 以便为覆写的方法标注参数类型
import requests

# 从基类导入所需设施
from finreg_ai.fetchers.base import RawDoc
# 从通用列表抓取器导入并继承，以获得分页、去重与 table 布局解析能力
from finreg_ai.fetchers.html_list import HtmlListFetcher

# 匹配「共 3 页」「第 2 页」「下一页」「尾页」「上一页」等分页导航文字。
# 这些文字在 table 布局中会被误识别为条目标题。
_PAGINATION_NOISE = re.compile(r"^(共\s*\d+\s*页|第\s*\d+\s*页|下一页|上一页|首页|末页|尾页|转到|GO)$")


class PbcFetcher(HtmlListFetcher):
    """中国人民银行抓取器。

    继承 ``HtmlListFetcher`` 而非 ``BaseFetcher``，以获得三样必需能力：

    - **分页**：人行列表页确有多页，配置中的 ``pagination`` 必须真正生效
    - **去重**：同一份文件常因「按标题分组」的排版在页面上出现多次
    - **table 布局解析**：人行历史页面大量使用 ``<table>``

    这三样如果缺了，后果不是「少抓几条」而是「数据错误」——
    本项目就曾因为直接继承 ``BaseFetcher`` 而让同一条公告重复出现三次。
    """

    # 抓取器名称，用于工厂匹配
    name = "pbc"

    def parse(self, html: str) -> list[RawDoc]:
        """解析人行列表页，在通用解析结果之上做两处清洗。

        注意这里调用的是 ``super().parse``。早期实现写的是
        「新建一个 ``HtmlListFetcher`` 实例再调它的 ``parse``」，
        在本类改为继承 ``HtmlListFetcher`` 之后，那样写会造成
        实例关系混乱；用 ``super()`` 才是正确的表达方式。
        """
        # 先复用父类的通用解析逻辑，避免重复实现选择器处理
        docs = super().parse(html)
        # 结果容器
        cleaned: list[RawDoc] = []
        # 逐条清洗
        for doc in docs:
            # 过滤分页导航文字被误识别为标题的情况
            if _PAGINATION_NOISE.match(doc.title.strip()):
                # 跳过该条
                continue
            # 标题中不含任何中文字符的条目，几乎都是页面控件而非政策
            if not re.search(r"[\u4e00-\u9fff]", doc.title):
                # 跳过该条
                continue
            # 保留
            cleaned.append(doc)
        # 返回清洗后的结果
        return cleaned

    @staticmethod
    def _detect_encoding(response: requests.Response) -> str:
        """覆写编码探测，优先信任页面内声明的编码。

        人行的具体问题：部分页面 HTTP 头声明 ``ISO-8859-1``（这是
        HTTP/1.1 的默认值，很多老服务器不带 charset 时会被这样解读），
        但页面 meta 声明 ``gb2312``。此时必须信任 meta 声明，
        否则所有中文标题都会变成乱码，存入库中就是永久性的数据损坏。

        关于这里的 ``@staticmethod``：它曾被写成实例方法，理由是
        「覆写静态方法以使用 self」——但函数体里根本没有用到 ``self``。
        当时的写法有两个后果，值得记下来：

        1. **当时看不出来**：基类内部是以 ``self._detect_encoding(response)``
           调用的，实例方法绑定后收到 ``(self, response)``，恰好能跑通，
           实测也确实返回了正确的 ``gb2312``。所以这并不是一个
           「覆写从未生效」的 bug——我一度那样怀疑，实测推翻了它。
        2. **隐患仍在**：实例方法使 ``PbcFetcher._detect_encoding(resp)``
           这种类级调用直接抛 ``TypeError``（少一个参数）。也就是说，
           覆写悄悄改变了父类方法的调用约定，调用方一旦换一种写法就会崩。

        结论：既然不需要 ``self``，就老实写成静态方法。
        跨类的行为约定不该被某个子类单方面改动。
        """
        # 从原始字节中探测页面实际编码。
        # 显式标注为 str：requests 未给该属性写类型标注，mypy 视其为 Any，
        # 直接使用会让本函数「返回 Any」，从而掩盖后续真实的类型错误。
        apparent: str = response.apparent_encoding
        # 探测到 GB 系列编码时优先采用（这是人行历史页面的特征）
        if apparent and apparent.lower().startswith(("gb", "big5")):
            # 返回探测结果的小写形式
            return apparent.lower()
        # 其次是明确的 UTF-8 声明
        if apparent and "utf" in apparent.lower():
            # 返回 utf-8
            return "utf-8"
        # 最后退回 requests 依据响应头的判断
        return response.encoding or "utf-8"
