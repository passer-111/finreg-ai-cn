"""国家互联网信息办公室（CAC）专用抓取器。

为什么这是本项目最关键的数据源
------------------------------
网信办是 AI 专项法规的主要发文机构，且金融机构的生成式 AI 备案也在此办理：

- 《生成式人工智能服务管理暂行办法》（2023-08 施行）
- 《互联网信息服务算法推荐管理规定》（2022-03 施行）
- 《互联网信息服务深度合成管理规定》（2023-01 施行）
- 《人工智能生成合成内容标识办法》（2025-09 施行）
- 《人工智能拟人化互动服务管理暂行办法》（2026-07 施行）

因此该源与金融 AI 合规高度耦合——金融机构要用生成式 AI，必然同时受
网信办的备案要求与金融监管总局的业务合规要求双重约束。

特殊之处：分页语义与大多数网站相反
----------------------------------
网信办的分页地址是 ``A093601index_1.htm``、``A093601index_2.htm``……
其中 ``index_1`` **就是第一页**。

而多数中国政务网站的模式是「首页无页码后缀，第二页才是 index_1」。
这个差异必须由专用抓取器处理，否则会漏掉第一页的全部内容——
这是一个隐蔽且危险的错误：抓取看似成功，却静默丢失最新政策。

实测网络可达性：在部分网络环境下超时不可达（http=000），
因此本抓取器必须有 fixture 驱动的测试覆盖。
"""

# 导入 Any 类型标注
from typing import Any

# 从基类导入所需设施
from finreg_ai.fetchers.base import BaseFetcher, RawDoc
# 从通用抓取器导入通用实现
from finreg_ai.fetchers.html_list import HtmlListFetcher


class CacFetcher(BaseFetcher):
    """网信办抓取器，处理 1 基页面的分页语义。"""

    # 抓取器名称，用于工厂匹配
    name = "cac"

    def _page_url(self, page: int, pagination: dict[str, Any]) -> str:
        """构造第 page 页的 URL。

        与通用实现的关键差异：网信办的 ``index_1`` 是第一页，
        因此页码直接用 ``page`` 本身，而不像通用实现那样用 ``page - 1``。

        这个差异如果处理错，第一页的内容会永远抓不到，
        而抓取结果看起来完全正常——这是最难察觉的一类 bug。
        """
        # 取出 URL 模板
        pattern = pagination.get("url_pattern")
        # 未配置模板时退回列表页地址（此时只有一页）
        if not pattern:
            # 返回配置的列表页地址
            return self.source.list_url or ""
        # 用列表页地址的目录部分作为前缀
        base = (self.source.list_url or "").rsplit("/", 1)[0]
        # 页码直接从 1 开始，不做偏移
        concrete = pattern.replace("{page}", str(page))
        # 拼接完整地址
        return f"{base}/{concrete}"

    def parse(self, html: str) -> list[RawDoc]:
        """解析网信办列表页。

        除通用解析外，额外做一处针对性过滤：网信办列表页含大量
        「政策解读」「图解」等衍生内容。这些内容本身有价值，
        但不是政策原文，不应作为政策记录入库——
        它们的 URL 通常包含特定路径片段，据此识别过滤。
        """
        # 复用通用解析逻辑
        docs = HtmlListFetcher(self.source, session=self.session).parse(html)
        # 结果容器
        kept: list[RawDoc] = []
        # 逐条过滤
        for doc in docs:
            # 排除解读与图解密生内容：这类内容不是规范性文件本身，
            # 混入会导致「同一政策出现两条记录」，破坏数据完整性
            if any(marker in doc.url for marker in ("/zhuanti/", "jiedu", "tujie")):
                # 跳过该条
                continue
            # 保留
            kept.append(doc)
        # 返回过滤结果
        return kept
