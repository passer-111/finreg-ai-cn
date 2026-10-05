"""pytest 共享装置 —— 提供离线测试所需的一切基础设施。

设计目标
--------
本项目的抓取对象是政府网站，它们**不可靠也不该被测试依赖**：
会超时、会改版、会对 CI 的 IP 限流。因此测试必须在完全离线的条件下
跑通整条解析链路，方法是：

1. 用 ``tests/fixtures/`` 下的固定装置替代真实响应；
2. 用 ``FakeSession`` 替代 ``requests.Session``，按 URL 路由到固定装置；
3. 把 ``min_interval_seconds`` 设为 0，避免测试因限速而变慢。

这样 CI 既不需要外网，也不会给监管机构服务器造成压力——
后者是本项目作为长期基础设施必须守住的底线。

注意：需要真实网络的测试用 ``@pytest.mark.network`` 标记，
默认不在 CI 中运行（见 pyproject.toml 的 markers 配置）。
"""

# 导入 json 用于构造伪造的 JSON 响应
import json
# 导入 pathlib 用于定位固定装置目录
from pathlib import Path
# 导入容器抽象基类作为类型标注（来源为 collections.abc，理由见 cli.py 的说明）
from collections.abc import Callable
# 导入 Any 类型标注
from typing import Any

# 导入 pytest 以定义装置
import pytest

# 从本包导入数据源模型与转换工具
from finreg_ai.models import Source, SourceTier, source_from_dict

# 固定装置所在目录
FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ============================================================
# 伪造的 HTTP 响应
# ============================================================

class FakeResponse:
    """模拟 ``requests.Response`` 的最小实现。

    只实现抓取器真正会用到的那几个属性，而不是照抄整个 Response 接口。
    这样做的好处是：如果将来抓取器开始依赖新的响应属性，
    这里会因为缺属性而立刻报错——而不是静默地拿到一个 Mock 对象继续跑。
    """

    def __init__(
        self,
        text: str = "",                 # 响应正文
        status_code: int = 200,         # HTTP 状态码
        encoding: str = "utf-8",        # 响应头声明的编码
        apparent_encoding: str = "utf-8",  # 从内容探测出的编码
    ) -> None:
        """按参数构造伪造响应。"""
        # 保存正文
        self.text = text
        # 保存状态码
        self.status_code = status_code
        # 保存声明的编码
        self.encoding = encoding
        # 保存探测出的编码
        self.apparent_encoding = apparent_encoding
        # 响应头，抓取器目前不用，但保留以免将来 AttributeError
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        """把正文按 JSON 解析后返回。

        若正文不是合法 JSON，会抛出 ``json.JSONDecodeError``——
        这正是抓取器需要处理的场景之一（被重定向到 HTML 页面），
        因此这里不能吞掉异常。
        """
        # 直接委托给标准库，保留原始异常类型
        return json.loads(self.text)


# 路由函数的类型：接收完整 URL，返回伪造响应
RouteFn = Callable[[str], FakeResponse]


class FakeSession:
    """按 URL 路由到固定装置的伪造会话。

    取代 ``requests.Session`` 注入给抓取器，从而阻断一切真实网络请求。
    每次请求都会被记录在 ``calls`` 中，便于断言「抓取器发了几次请求、
    请求了哪些地址」——这对分页逻辑的测试尤其有用。
    """

    def __init__(self, route: RouteFn) -> None:
        """保存路由函数。"""
        # 路由函数：URL 字符串 -> FakeResponse
        self._route = route
        # 请求记录列表，元素形如 ("GET", url)
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        """处理 GET 请求：记录后交给路由函数。

        忽略 kwargs（timeout / headers 等）——伪造会话不需要它们，
        但保留参数是为了与 requests 的调用签名兼容。
        """
        # 记录本次请求
        self.calls.append(("GET", url))
        # 交给路由函数决定返回什么
        return self._route(url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        """处理 POST 请求：记录后交给路由函数。"""
        # 记录本次请求
        self.calls.append(("POST", url))
        # 交给路由函数决定返回什么
        return self._route(url)


# ============================================================
# 固定装置加载
# ============================================================

def load_fixture_text(name: str) -> str:
    """读取 ``tests/fixtures/`` 下的文本文件。"""
    # 拼接路径并读取，显式声明 UTF-8 以免受平台默认编码影响
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> Any:
    """读取并解析 ``tests/fixtures/`` 下的 JSON 文件。"""
    # 复用文本读取后解析
    return json.loads(load_fixture_text(name))


# ============================================================
# 构造测试用数据源
# ============================================================

# 测试用数据源的默认字段。刻意让 min_interval_seconds 为 0，
# 否则每次请求前都会真实休眠，测试会变得很慢。
_DEFAULT_SOURCE: dict[str, Any] = {
    "id": "test-source",              # 源标识
    "issuer_code": "nfra",            # 关联机构（仅为满足必填，不参与校验）
    "name": "测试数据源",              # 源名称
    "tier": "primary",                # 来源等级
    "enabled": True,                  # 启用
    "fetcher": "json_search_list",    # 默认抓取器
    "list_url": "https://example.gov.cn/list/index.html",  # 列表页地址
    "schedule": "0 6 * * *",          # 抓取频率
    "min_interval_seconds": 0,        # 不限速，避免测试变慢
    "pagination": None,               # 无分页配置
    "selectors": None,                # 无选择器
    "api": None,                      # 无接口配置
    "penalty": None,                  # 无详情页表格配置
    "filters": None,                  # 无过滤规则
    "notes": None,                    # 无备注
}


def make_source(**overrides: Any) -> Source:
    """构造一个测试用 ``Source``，可用关键字参数覆盖任意字段。

    例：``make_source(fetcher="html_list", list_url="http://a.cn/x/index.html")``
    """
    # 在默认值基础上应用覆盖项
    data = {**_DEFAULT_SOURCE, **overrides}
    # 复用生产代码的转换函数，保证测试数据与真实数据走同一套构造逻辑
    source = source_from_dict(data)
    # 断言等级为 primary——本项目的 schema 也只允许 primary 进入记录
    assert source.tier is SourceTier.PRIMARY
    # 返回构造好的数据源
    return source


# ============================================================
# 常用装置
# ============================================================

@pytest.fixture
def fixtures_dir() -> Path:
    """返回固定装置目录，供需要直接读取文件的测试使用。"""
    # 直接返回模块级常量
    return FIXTURES_DIR
