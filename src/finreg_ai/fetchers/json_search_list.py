"""通用 JSON 接口抓取器 —— 覆盖中国政府 CMS 的 /searchList 接口族。

为什么这是本项目的主力抓取器
----------------------------
实测验证了一个关键事实：**中国政府监管网站的列表页绝大多数由
JavaScript 渲染**，纯 HTML 抓取拿不到任何条目。

具体验证结果（2026-10-03 实测）：

===================  ==============  ==========================================
站点                  列表页 HTML      真实数据来源
===================  ==============  ==========================================
csrc.gov.cn          仅 10 KB 空壳    /searchList/{channelId} 接口，共 234 条
nfra.gov.cn          仅 4 KB 空壳     /cbircweb/DocInfo/SelectDocByItemIdAndChild，共 4888 条
pbc.gov.cn           table 布局可用    table tr + a + td:last-child（服务端渲染，无需接口）
cac.gov.cn           不可达（http=000）  —
===================  ==============  ==========================================

因此 HTML 选择器方案的适用面远比预想的小。真正可复用的是**接口调用模式**：
这些站点大量复用同一套政府 CMS，接口形如：

    /searchList/{channelId}?_isAgg=true&_isJson=true&_pageSize=18&_template=index&page=1

本抓取器把这套模式全部参数化（见 schema/source.schema.json 的 api 配置节），
因此接入一个同族新站点只需在 YAML 中填几行配置，无需写代码。

风险与应对
----------
这类内部接口没有公开文档，可能随时变更。因此本抓取器遵循两条原则：

1. **任何异常都降级而非崩溃。** 接口改了、字段改名了、返回 HTML 了，
   都以 degraded 状态返回并说明原因，绝不让单个源拖垮流水线。
2. **结果质量自检。** 解析出条目后检查质量（标题长度、日期命中率、
   链接有效性），发现异常时主动降级，而不是把导航链接当作政策存进库。

第 2 条尤其重要。本项目早期版本曾因为选择器过宽，把「京ICP备…」
和「统计数据」当成政策抓了进来，而流水线报告 status=ok ——
**静默污染比明确失败危险得多**。
"""

# 导入 json 用于解析接口响应
import json
# 导入 re 用于渲染 URL 模板中的占位符
import re
# 导入 date 与 datetime 用于时间戳转换。
# 时区（CHINA_TZ）从 base 模块导入，此处不重复导入 timezone / timedelta，
# 以免出现两套时区定义导致跨日边界处理不一致。
from datetime import date, datetime
# 导入 Any 类型标注
from typing import Any

# 从基类导入所需设施
from finreg_ai.fetchers.base import CHINA_TZ, BaseFetcher, RawDoc, dedupe_by_url
# 从通用抓取器导入日期解析与标题清理工具
from finreg_ai.fetchers.html_list import clean_title, parse_chinese_date


# 匹配 URL 模板中的 ``{字段名}`` 占位符
_URL_TEMPLATE_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


def render_item_template(template: str, item: dict[str, Any]) -> str | None:
    """用条目自身的字段填充 URL 模板；字段缺失时返回 None。

    为什么需要这个能力
    ------------------
    政府 CMS 的接口不一定直接给出可点击的详情页链接。例如金融监管总局的
    列表接口只返回 ``docId`` 与文档文件路径，没有 HTML 详情页地址——
    详情页要靠 ``ItemDetail.html?docId=xxx`` 这样的规则拼出来。

    与其为一个站点写一个专用抓取器，不如把「拼装规则」做成配置项，
    这样同类站点都能复用。这符合本项目「能用配置解决就不写代码」的取向。

    参数 template 形如 ``https://…/ItemDetail.html?docId={docId}``；
    参数 item 是接口返回的单条记录。
    """
    # 空模板无法渲染
    if not template:
        # 返回 None
        return None

    def _replace(match: re.Match[str]) -> str:
        """把单个 ``{字段名}`` 替换为条目中对应的值。"""
        # 取出字段名
        key = match.group(1)
        # 取值
        value = item.get(key)
        # 字段缺失或值为 None 时，**原样保留占位符**而不是替换成空串。
        #
        # 这一点是刻意的，也是本项目实际踩过的坑：若替换成空串，
        # 模板 ``…?docId={docId}`` 会渲染成 ``…?docId=``——
        # 一个看起来完全正常、实际打不开的链接。下方的花括号检查
        # 本来是专门用来拦这类残缺链接的，替换成空串会让它失效。
        # 保留占位符后，检查逻辑就能正常工作。
        if value is None:
            # 返回原始占位符，交由调用方判定为渲染失败
            return match.group(0)
        # 转为字符串返回
        return str(value)

    # 执行替换
    rendered = _URL_TEMPLATE_PLACEHOLDER.sub(_replace, template)

    # 若替换后仍残留花括号，说明模板里引用了条目中不存在的字段。
    # 此时必须返回 None 而不是把残缺的 URL 交出去——
    # 一个 ``…?docId=`` 的空参数链接会 200 通过校验，但打不开，
    # 而且会被当作「有效的官方链接」写进政策记录，属于不可逆的数据污染。
    if "{" in rendered or "}" in rendered:
        # 渲染失败
        return None

    # 返回渲染结果
    return rendered


def parse_epoch_timestamp(value: Any) -> date | None:
    """把 epoch 时间戳解析为日期；不是时间戳时返回 None。

    为什么要单独处理：中国政府 CMS 的 JSON 接口常同时返回两个日期字段——
    ``publishedTime``（epoch 毫秒整数）与 ``publishedTimeStr``（可读字符串）。
    若字段映射错指向了前者，把整数交给文本日期解析器会静默返回 None，
    导致全部条目丢失日期，而抓取状态仍显示 ok。

    本项目已实际踩过这个坑，因此在这里做显式处理，
    并且对秒级与毫秒级时间戳都做兼容（10 位为秒，13 位为毫秒）。

    注意：转换时按北京时间解释，因为政府网站的发布时间均为 UTC+8。
    若按 UTC 解释，会导致日期在跨日边界上偏移一天。
    """
    # 整数直接使用
    if isinstance(value, bool):
        # 布尔是 int 的子类，必须先行排除，否则 True 会被当成 1 秒
        return None
    # 数值型时间戳。这里用 X | Y 而非 (X, Y) 元组形式：
    # 后者是 Python 3.10 之前为兼容 isinstance 而采用的写法，现已无必要。
    if isinstance(value, int | float):
        # 转为整数使用
        number = int(value)
    # 纯数字字符串也视为时间戳
    elif isinstance(value, str) and value.strip().isdigit():
        # 转成整数
        number = int(value.strip())
    else:
        # 其它类型（含可读日期字符串）不按时间戳处理
        return None

    # 数值过小说明不是合理的时间戳（1970 年附近的秒数不足 9 位）
    if number < 10_000_000:
        # 不是有效时间戳
        return None
    # 13 位及以上按毫秒处理，10 位按秒处理
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        # 按北京时间解释该时间戳
        moment = datetime.fromtimestamp(seconds, tz=CHINA_TZ)
    except (OverflowError, OSError, ValueError):
        # 超出可表示范围时返回 None，不猜测
        return None
    # 返回日期部分
    return moment.date()


class JsonSearchListFetcher(BaseFetcher):
    """配置驱动的 JSON 接口抓取器。

    全部行为由数据源配置中的 ``api`` 节决定：
    接口路径、请求参数、字段映射、结果数组位置。
    """

    # 抓取器名称，用于工厂匹配
    name = "json_search_list"

    # 单次抓取允许的最大页数，防止配置错误导致无限翻页
    ABSOLUTE_MAX_PAGES = 8

    # 结果质量自检阈值：标题平均长度低于此值，说明很可能抓到了导航链接
    MIN_AVG_TITLE_LENGTH = 8

    # 结果质量自检阈值：日期命中率低于此比例，说明字段映射可能已失效
    MIN_DATE_HIT_RATE = 0.3

    def _collect(self) -> tuple[list[RawDoc], str]:
        """分页调用 JSON 接口，收集全部条目。"""
        # 取出接口配置
        api = self.source.api or {}
        # 缺少接口配置时无法工作，返回空使上层标记 degraded
        if not api.get("path"):
            # 返回空结果
            return [], ""

        # 逐页请求
        all_docs: list[RawDoc] = []
        # 取出每页条数
        page_size = int(api.get("page_size", 18) or 18)
        # 计算页数上限
        max_pages = self.ABSOLUTE_MAX_PAGES

        # 遍历页码
        for page in range(1, max_pages + 1):
            # 请求本页
            payload = self._request_page(api, page, page_size)
            # 请求失败：首页失败说明接口不可用，抛异常让上层标记 failed
            if payload is None:
                # 首页失败则整体失败
                if page == 1:
                    # 抛出带上下文异常
                    raise RuntimeError(
                        f"JSON 接口请求失败：{self._build_url(api)}（{self._last_error or '未知原因'}）"
                    )
                # 后续页失败时停止翻页，保留已有结果
                break

            # 提取条目数组
            items = self._extract_items(payload, api)
            # 本页无条目说明已到末页
            if not items:
                # 停止翻页
                break

            # 转换条目
            for item in items:
                # 转换为 RawDoc，失败返回 None
                doc = self._item_to_doc(item, api)
                # 仅保留成功转换的
                if doc is not None:
                    # 追加
                    all_docs.append(doc)

            # 本页条数少于每页条数，说明已是末页
            if len(items) < page_size:
                # 停止翻页
                break

        # 去重（分页边界可能重复）
        deduped = self._dedupe_by_url(all_docs)
        # 执行质量自检：不合格时返回空结果，让上层标记 degraded 而非 ok
        if not self._passes_quality_check(deduped):
            # 返回空列表触发 degraded 状态，避免把垃圾数据当成功结果
            return [], ""

        # 返回结果（第二个返回值为空，因为本抓取器没有 HTML 可供调试）
        return deduped, ""

    def _request_page(self, api: dict[str, Any], page: int, page_size: int) -> dict[str, Any] | None:
        """请求指定页并返回解析后的 JSON；失败返回 None。"""
        # 构造完整 URL
        url = self._build_url(api, page=page, page_size=page_size)
        # 取出 HTTP 方法，默认 GET
        method = str(api.get("method", "GET")).upper()

        try:
            # 请求前限速，对监管服务器保持礼貌
            self._throttle()
            # 按方法发起请求。带上 Referer 指向列表页是稳妥做法：
            # 多数站点不校验，但少数站点会以 403 拒绝缺少来源头的请求。
            # 实测结论修正（2026-10-03）：nfra.gov.cn 并不校验 Referer，
            # 早期认为该站「有 403 反爬」是对错误接口地址的误判——
            # 用对地址后无需任何额外请求头即可正常取数。
            headers = {"Referer": self.source.list_url or (self.source.api or {}).get("path", "")}
            # GET 请求把参数放在查询串中
            if method == "GET":
                # 发起 GET
                response = self.session.get(url, timeout=20, headers=headers)
            else:
                # POST 请求把参数放在表单体中，查询串只保留路径
                # 注意：此处不再重复拼接参数，由 _build_url 的 query 部分处理
                base, _, query = url.partition("?")
                # 解析查询串为字典
                form = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
                # 发起 POST
                response = self.session.post(base, data=form, timeout=20, headers=headers)
            # 更新限速计时
            self._last_request_at = __import__("time").monotonic()
            # 累计请求数
            self._request_count += 1
            # 记录 HTTP 状态码，供上层诊断
            self._last_http_status = response.status_code
            # 状态码非 200 视为失败
            if response.status_code != 200:
                # 记录失败原因
                self._last_error = f"HTTP {response.status_code}"
                # 返回 None
                return None
            # 解析 JSON。
            # 显式做一次「顶层必须是对象」的检查，而不是直接 return response.json()：
            # 后者在类型上会带出 Any，更重要的是——若接口返回的是数组或标量
            # （例如被重定向到一个正常返回 200 但内容完全无关的接口），
            # 后面按 items_path 取值会得到 None，最终表现为「解析到 0 条」，
            # 也就是本项目最忌讳的静默降级：看起来是源没更新，其实是抓错了东西。
            parsed: Any = response.json()
            # 顶层不是对象则判定为接口变更，明确失败
            if not isinstance(parsed, dict):
                # 记录失败原因，写清楚实际拿到的类型
                self._last_error = f"接口返回的 JSON 顶层不是对象，而是 {type(parsed).__name__}"
                # 返回 None 让上层标记该源失败
                return None
            # 返回解析结果
            return parsed
        except json.JSONDecodeError as exc:
            # 返回的不是 JSON（很可能被重定向到了 HTML 页面）
            self._last_error = f"响应非 JSON（可能被重定向或触发反爬）：{exc}"
            # 返回 None
            return None
        except Exception as exc:  # noqa: BLE001  任何异常都降级处理，保证单源隔离
            # 记录异常类型与内容
            self._last_error = f"{type(exc).__name__}: {exc}"
            # 返回 None
            return None

    def _build_url(self, api: dict[str, Any], page: int = 1, page_size: int | None = None) -> str:
        """根据配置构造完整的接口 URL。"""
        # 站点根地址：从列表页地址中取协议与域名部分
        base = self._site_root()
        # 接口路径
        path = str(api.get("path", ""))
        # 替换 channel_id 占位符
        path = path.replace("{channel_id}", str(api.get("channel_id") or ""))
        # 取出参数模板
        params: dict[str, str] = dict(api.get("params") or {})
        # 替换参数值中的页码与每页条数占位符
        resolved: list[str] = []
        # 逐项处理参数
        for key, value in params.items():
            # 值转字符串后替换占位符
            text = str(value).replace("{page}", str(page)).replace("{page_size}", str(page_size or api.get("page_size", 18)))
            # 拼成 key=value 形式
            resolved.append(f"{key}={text}")
        # 拼接完整 URL
        return f"{base}{path}?{'&'.join(resolved)}" if resolved else f"{base}{path}"

    def _site_root(self) -> str:
        """从列表页地址中提取协议与域名，作为接口地址的前缀。"""
        # 取出列表页地址
        url = self.source.list_url or ""
        # 按斜杠切分，取前三个部分（scheme://host）
        parts = url.split("/")
        # 标准情况下前三个元素即 protocol、空串、host
        if len(parts) >= 3 and parts[0].startswith("http"):
            # 拼回根地址
            return f"{parts[0]}//{parts[2]}"
        # 无法解析时退回空串，接口 URL 将只有路径部分（通常会失败，由上层降级）
        return ""

    @staticmethod
    def _extract_items(payload: Any, api: dict[str, Any]) -> list[dict[str, Any]]:
        """按配置的路径从响应中取出条目数组。

        路径形如 ``data.results``，逐层下钻。
        若配置的路径取不到数组，会尝试若干常见的备用路径——
        因为不同站点的这层结构命名差异很大（results / rows / list / records）。
        """
        # 取出配置的结果路径
        configured = str(api.get("items_path", "data.results"))
        # 沿着路径逐层下钻
        node: Any = payload
        # 按点号切分路径
        for key in configured.split("."):
            # 非字典则无法继续下钻
            if not isinstance(node, dict):
                # 中断下钻
                node = None
                # 跳出循环
                break
            # 取下一层
            node = node.get(key)
        # 命中数组且非空则返回其中字典元素
        if isinstance(node, list) and node:
            # 过滤非字典元素
            return [item for item in node if isinstance(item, dict)]

        # 配置路径失效时尝试常见备用路径，提高对接口微调的容忍度
        for fallback in ("data.results", "data.rows", "data.list", "data.records", "results", "rows", "list"):
            # 逐层下钻
            cursor: Any = payload
            # 遍历路径分段
            for key in fallback.split("."):
                # 类型守卫
                if not isinstance(cursor, dict):
                    # 中断
                    cursor = None
                    # 跳出
                    break
                # 下钻一层
                cursor = cursor.get(key)
            # 命中数组则返回
            if isinstance(cursor, list) and cursor:
                # 过滤非字典元素后返回
                return [item for item in cursor if isinstance(item, dict)]

        # 全部失败返回空
        return []

    def _item_to_doc(self, item: dict[str, Any], api: dict[str, Any]) -> RawDoc | None:
        """把单个 JSON 条目转为 ``RawDoc``。

        字段名通过 ``api.field_map`` 映射，因此不同站点的命名差异
        （docTitle vs title、publishDate vs publishedTime）只需改配置。
        """
        # 取出字段映射
        field_map: dict[str, str] = dict(api.get("field_map") or {})
        # 解析出标题字段名，默认 title
        title_key = field_map.get("title", "title")
        # 解析出链接字段名，默认 url
        url_key = field_map.get("url", "url")
        # 解析出日期字段名，默认 publishedTime
        date_key = field_map.get("date", "publishedTime")

        # 取标题原文
        raw_title = item.get(title_key)
        # 标题缺失或过短则丢弃该条
        if not raw_title or not isinstance(raw_title, str):
            # 无法使用
            return None
        # 清理标题中的排版噪声
        title = clean_title(raw_title)
        # 清理后过短说明抓到的是非标题元素
        if len(title) < 4:
            # 丢弃
            return None

        # 取链接
        raw_url = item.get(url_key)
        # 分为两种取链接方式：
        # ① 接口直接给了链接字段（如证监会返回的 url）；
        # ② 没有链接字段，但配置了 url_template，需要从其它字段拼装
        #    （如金融监管总局只给 docId，详情页地址需按规则拼）。
        if isinstance(raw_url, str) and raw_url.strip():
            # 方式 ①：直接使用接口给出的链接
            candidate = raw_url.strip()
            # 相对链接补全为绝对地址
            url = candidate if candidate.startswith(("http://", "https://")) else self._absolutize(candidate)
        else:
            # 方式 ②：按模板拼装
            url_template = str(api.get("url_template") or "")
            # 渲染模板
            rendered = render_item_template(url_template, item)
            # 渲染失败说明该条缺少模板所需的字段，丢弃该条。
            # 这里刻意返回 None 而不是造一个残缺链接——
            # 可核验性优先于条目数量：一个打不开的「官方链接」比没有链接危害更大。
            if not rendered:
                # 无法使用
                return None
            # 使用渲染结果
            url = rendered

        # 解析日期；接口返回的可能是可读字符串（2026-09-11 18:10:17），
        # 也可能是 epoch 时间戳（毫秒或秒），需分别处理
        raw_date = item.get(date_key)
        # 优先按 epoch 时间戳解析——不少政府 CMS 同时提供
        # publishedTime（epoch 毫秒）与 publishedTimeStr（可读字符串）两个字段，
        # 若配置误指向了前者，静默解析成 None 会让所有条目丢失日期
        published = parse_epoch_timestamp(raw_date)
        # epoch 解析失败时再按文本日期解析
        if published is None:
            # 按中文/ISO 日期格式解析
            published = parse_chinese_date(str(raw_date)) if raw_date else None

        # 组装条目
        return RawDoc(
            title=title,                    # 清理后的标题
            url=url,                        # 绝对链接
            source_id=self.source.id,       # 来源标识
            published_on=published,         # 发布日期
        )

    def _passes_quality_check(self, docs: list[RawDoc]) -> bool:
        """对解析结果做质量自检，判断是否可信。

        为什么需要这一步：抓取器最危险的失败方式不是崩溃，而是
        「成功地」抓到一堆垃圾。本项目早期版本就因为选择器过宽，
        把页脚备案号和统计数据当作政策入库，而状态报告仍是 ok。

        两个检查维度：
        - **标题平均长度**：政策标题通常较长（十几个字）。若平均长度过短，
          说明抓到的大概率是导航项、按钮文字或页脚链接。
        - **日期命中率**：政策条目几乎总有发布日期。若大部分条目无日期，
          说明日期字段映射已失效，或抓到的根本不是列表数据。

        自检不通过时返回 False，由调用方把状态标记为 degraded 并提示人工确认。
        宁可多一次人工确认，也不要让垃圾数据静默入库。
        """
        # 空结果不算「通过』——由调用方按 degraded 处理
        if not docs:
            # 返回 False 触发降级
            return False
        # 计算标题平均长度
        avg_title_len = sum(len(doc.title) for doc in docs) / len(docs)
        # 平均长度过短说明抓到的不是政策标题
        if avg_title_len < self.MIN_AVG_TITLE_LENGTH:
            # 记录原因供上层展示
            self._last_error = f"结果质量自检未通过：标题平均长度仅 {avg_title_len:.1f} 字，疑似抓取到导航链接或页脚内容"
            # 不合格
            return False
        # 计算日期命中率
        dated = sum(1 for doc in docs if doc.published_on is not None)
        # 命中率
        hit_rate = dated / len(docs)
        # 命中率过低说明日期字段映射可能已失效
        if hit_rate < self.MIN_DATE_HIT_RATE:
            # 记录原因
            self._last_error = f"结果质量自检未通过：仅 {hit_rate:.0%} 的条目标题能解析出日期，日期字段映射可能已失效"
            # 不合格
            return False
        # 全部通过
        return True

    @staticmethod
    def _dedupe_by_url(docs: list[RawDoc]) -> list[RawDoc]:
        """按链接去重，保持首次出现顺序。

        实现委托给基类的 ``dedupe_by_url``——去重逻辑收敛到一处，
        避免各子类各写一份导致行为分叉。保留该方法名以不破坏既有调用点。
        """
        # 委托给基类的统一实现
        return dedupe_by_url(docs)

    def parse(self, html: str) -> list[RawDoc]:
        """HTML 解析入口。

        JSON 抓取器不解析 HTML——这个方法的存在只是为了满足基类的抽象要求。
        若走到了这里，说明 HTML 回退路径被调用，直接返回空。
        """
        # 明确不支持，返回空列表
        return []
