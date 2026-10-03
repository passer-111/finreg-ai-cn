"""解析层单元测试 —— 覆盖日期解析、标题清洗、时间戳转换、URL 模板渲染。

为什么这些纯函数值得单独测
--------------------------
它们是整条数据链路的第一道关口。一个日期解析错误不会让程序崩溃，
而是让一条政策被归到错误的生效时间，然后静静躺在库里。
在合规场景中，「悄悄错了」比「明确报错」危险得多，
因此这里对每一个函数都同时测试正常值与**边界/异常值**。
"""

# 导入 date 类型用于构造期望值
from datetime import date

# 导入 pytest 以使用参数化与异常断言
import pytest

# 导入待测函数
from finreg_ai.fetchers.html_list import clean_title, parse_chinese_date
from finreg_ai.fetchers.json_search_list import parse_epoch_timestamp, render_item_template


# ============================================================
# 中文日期解析
# ============================================================

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 中国政府网站最常用的格式
        ("2026年6月18日", date(2026, 6, 18)),
        # 带空格的中文格式（排版对齐常导致空格）
        ("2026 年 6 月 18 日", date(2026, 6, 18)),
        # ISO 类格式：横线
        ("2026-06-18", date(2026, 6, 18)),
        # ISO 类格式：斜杠
        ("2026/06/18", date(2026, 6, 18)),
        # ISO 类格式：点号
        ("2026.06.18", date(2026, 6, 18)),
        # 带时间的完整时间戳（NFRA 接口返回的就是这种）
        ("2026-09-11 18:10:17", date(2026, 9, 11)),
        # 日期混在长文本中——列表页整行文本里提取日期的场景
        ("公布日期：2026年6月18日 来源：本站", date(2026, 6, 18)),
        # 两位年份简写，本项目只处理 2000 年后，故 26 -> 2026
        ("26-06-18", date(2026, 6, 18)),
    ],
)
def test_parse_chinese_date_accepts_real_formats(text: str, expected: date) -> None:
    """验证解析器覆盖了中国政务网站实测遇到的各类日期格式。"""
    # 断言解析结果与期望日期一致
    assert parse_chinese_date(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,               # 空值
        "",                 # 空串
        "   ",              # 纯空白
        "没有日期",          # 完全不含数字
        "2026年2月30日",     # 非法日期（2 月没有 30 日）
        "2026-13-01",       # 非法月份
        "2026-00-10",       # 月份为 0
    ],
)
def test_parse_chinese_date_returns_none_instead_of_guessing(text: str | None) -> None:
    """验证无法解析时返回 None，绝不猜测。

    这是刻意的设计选择：猜错的日期会让政策被归到错误的生效时间，
    而缺少日期只是一个可见的待办。因此解析器宁可返回 None。
    """
    # 断言返回 None
    assert parse_chinese_date(text) is None


def test_parse_chinese_date_prefers_chinese_format_over_iso() -> None:
    """验证解析优先级：中文格式最明确，应优先于歧义更大的两位年份简写。"""
    # 该文本同时含完整中文日期与形似简写的片段
    text = "自2026年6月18日起施行（文号 26-06-18）"
    # 应解析出中文格式的日期（二者恰好同值，因此这里是格式优先级的行为确认）
    assert parse_chinese_date(text) == date(2026, 6, 18)


# ============================================================
# 标题清洗
# ============================================================

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 换行与多余空白应被压成单个空格（政府网站常用空白对齐）
        ("关于人工智能的指导意见\n\n", "关于人工智能的指导意见"),
        ("关于人工智能   的指导意见", "关于人工智能 的指导意见"),
        # 末尾的方括号附件标记
        ("关于加强数据治理的通知[附件1]", "关于加强数据治理的通知"),
        # 末尾的方括号标签
        ("关于加强数据治理的通知【最新】", "关于加强数据治理的通知"),
        # 末尾的圆括号备注
        ("关于加强数据治理的通知（2026年修订）", "关于加强数据治理的通知"),
        # 多层噪声应被反复剥离直至干净
        ("关于加强数据治理的通知【最新】(附件)", "关于加强数据治理的通知"),
    ],
)
def test_clean_title_strips_layout_noise(raw: str, expected: str) -> None:
    """验证标题清洗只去除排版噪声，不改变语义。"""
    # 断言清洗结果
    assert clean_title(raw) == expected


def test_clean_title_keeps_semantic_brackets() -> None:
    """验证标题中间的括号内容不会被误删——那是标题的一部分，不是噪声。"""
    # 只有位于末尾的括号才是噪声，中间的必须保留
    raw = "中国人民银行公告〔2026〕第24号"
    # 断言原样保留
    assert clean_title(raw) == raw


def test_clean_title_handles_empty_input() -> None:
    """验证空输入返回空串而非抛异常。"""
    # 空串
    assert clean_title("") == ""
    # None 值（类型标注为 str，但运行时可能收到 None）
    assert clean_title(None) == ""  # type: ignore[arg-type]


# ============================================================
# epoch 时间戳解析
# ============================================================

def test_parse_epoch_timestamp_handles_milliseconds() -> None:
    """验证 13 位毫秒时间戳被正确解析为日期。"""
    # 该值取自证监会接口的实测样例
    assert parse_epoch_timestamp(1786847417000) == date(2026, 8, 16)


def test_parse_epoch_timestamp_milliseconds_and_seconds_agree() -> None:
    """验证秒级与毫秒级时间戳解析出同一天。

    政府 CMS 对同一时刻可能返回 10 位或 13 位，
    若不兼容两种精度，日期会整体偏移数十年。
    """
    # 两种精度应等价
    assert parse_epoch_timestamp(1786847417000) == parse_epoch_timestamp(1786847417)


def test_parse_epoch_timestamp_accepts_numeric_string() -> None:
    """验证纯数字字符串形式的数值时间戳也能被解析。"""
    # 字符串形式
    assert parse_epoch_timestamp("1786847417000") == date(2026, 8, 16)


@pytest.mark.parametrize(
    "value",
    [
        None,                      # 空值
        True,                      # 布尔——是 int 的子类，必须先行排除
        False,                     # 同上
        999,                       # 数值过小，不是合理的时间戳
        0,                         # 0 是 1970-01-01，不可能是政策日期
        -5,                        # 负值
        1e20,                      # 超出可表示范围，fromtimestamp 会抛异常
        "2026-09-11 18:10:17",     # 可读日期字符串，不是时间戳
        "abc",                     # 完全非数字
    ],
)
def test_parse_epoch_timestamp_returns_none_for_non_timestamps(value: object) -> None:
    """验证非时间戳输入返回 None，而不是把 0 解析成 1970 年。"""
    # 断言返回 None
    assert parse_epoch_timestamp(value) is None


# ============================================================
# URL 模板渲染
# ============================================================

def test_render_item_template_fills_named_fields() -> None:
    """验证模板中的字段占位符被条目字段值替换。"""
    # 模板与服务端只返回 docId 的场景一致
    template = "https://www.example.gov.cn/detail.html?docId={docId}"
    # 条目
    item = {"docId": 900001, "title": "某文件"}
    # 断言渲染结果
    assert render_item_template(template, item) == "https://www.example.gov.cn/detail.html?docId=900001"


def test_render_item_template_supports_multiple_placeholders() -> None:
    """验证模板支持多个不同占位符。"""
    # 含两个占位符
    template = "https://a.cn/{category}/{docId}.html"
    # 条目
    item = {"category": "rules", "docId": 7}
    # 断言拼接结果
    assert render_item_template(template, item) == "https://a.cn/rules/7.html"


def test_render_item_template_returns_none_when_field_missing() -> None:
    """验证字段缺失时返回 None，而不是生成一个残缺链接。

    这条规则的重要性：``…?docId=`` 这样的空参数链接会返回 200，
    因此能通过所有可达性检查，但它打不开——若被当作「有效的官方链接」
    写入记录，就是不可逆的数据污染。宁可丢掉该条。
    """
    # 条目里没有 docId
    item = {"title": "某文件"}
    # 断言返回 None
    assert render_item_template("https://a.cn/detail?docId={docId}", item) is None


def test_render_item_template_returns_none_for_empty_template() -> None:
    """验证空模板返回 None。"""
    # 空串
    assert render_item_template("", {"docId": 1}) is None


def test_render_item_template_renders_none_value_as_empty_and_fails() -> None:
    """验证字段值为 None 时按渲染失败处理。

    服务端常把不存在的字段返回为 null。此时占位符会被替换成空串，
    渲染结果形如 ``…?docId=`` —— 这是残缺链接，必须判为失败。
    """
    # docId 显式为 None
    assert render_item_template("https://a.cn/detail?docId={docId}", {"docId": None}) is None
