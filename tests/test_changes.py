"""变更文件的写入与合并 —— 保证「同一天跑多次」不会互相覆盖。

为什么这个文件值得单独存在
--------------------------
变更文件按天命名的（``data/changes/YYYY-MM-DD.json``），因此「同一天再跑一次」
会写到同一个文件上。在实现合并之前，写入是**整份覆盖**：

    finreg fetch                      # 全天，产出 55 条
    finreg fetch --source csrc-regulations   # 只想重试一个源
    # → 文件只剩 2 条，另外 53 条无声消失

这件事的危害不在于「丢了几条数据」，而在于它**恰好发生在维护者排查问题的时刻**：
单源运行是最常用的调试动作，而调试结束后若不补跑一次全量，
当天的变更记录就永久少了。而变更文件是项目的核心输出——
「最近变了什么」正是使用者来这里的原因。

这组测试全部用 ``tmp_path``，不碰仓库里的真实变更文件。
"""

# 导入 json 用于构造与读取变更文件
import json

# 导入 date 用于构造固定的检测日期
from datetime import date

# 导入 pathlib
from pathlib import Path

# 导入待测函数与数据结构
from finreg_ai.diff import Change, ChangeSet
from finreg_ai.pipeline import change_identity, load_previously_discovered_urls, write_change_set


# 固定的检测日期。写死日期而不是用当天，是为了让测试与运行日期彻底解耦——
# 以「今天」为基准的测试会随时间自行变红，本项目已经因此吃过一次亏。
FIXED_DAY = date(2026, 10, 5)


def make_change(**overrides: object) -> Change:
    """构造一条最小可用的变更记录。"""
    # 默认值取「一切正常」的形态
    defaults: dict = {
        "change_type": "discovered",                            # 变更类型
        "policy_id": "pbc-2026-demo",                           # 受影响的记录标识
        "title": "某测试文书",                                   # 标题
        "detected_on": FIXED_DAY,                               # 检测日期
        "detail": "发现新条目待录入",                             # 变更说明
        "significance": "medium",                               # 重要性
        "url": "https://example.gov.cn/a.html",                 # 官方链接
    }
    # 应用覆盖项
    defaults.update(overrides)
    # 构造
    return Change(**defaults)  # type: ignore[arg-type]


def make_change_set(changes: list[Change], detected_on: date = FIXED_DAY) -> ChangeSet:
    """构造一个变更集合。"""
    # 直接组装
    return ChangeSet(detected_on=detected_on, changes=changes)


def read_document(path: Path) -> dict:
    """读取写出的变更文件。"""
    # 以 UTF-8 读取 JSON
    with open(path, encoding="utf-8") as fh:
        # 返回解析结果
        return json.load(fh)


# ============================================================
# 自然键
# ============================================================

def test_change_identity_ignores_title_and_detail() -> None:
    """验证自然键只看类型、标识、链接，不看标题与说明。

    为什么这样取：同一条文书的标题可能被重新清洗过（多一个空格、少一个省略号），
    说明文字也可能因模板调整而改变。若把它们计入自然键，同一条变更会被当成两条，
    变更文件里出现成对的重复记录——而计数会跟着一起虚高，看不出异常。
    """
    # 两条只有标题与说明不同的变更
    a = make_change(title="某测试文书", detail="发现新条目待录入")
    # 标题与说明都改了
    b = make_change(title="某测试文书（修订）", detail="换了一段说明文字")
    # 自然键必须相同
    assert change_identity(a) == change_identity(b)


def test_change_identity_distinguishes_url() -> None:
    """反向测试：链接不同的两条变更必须被区分开。

    一条公示文书下面有十几条当事人记录，它们共用 policy_id（文号），
    唯一的区别就在链接上。若自然键不含链接，这十几条会被合并成一条，
    抓取报告显示「1 条」，而实际丢了 12 条——正是本项目最警惕的静默丢失。
    """
    # 同一文号下的两条当事人记录
    a = make_change(url="https://example.gov.cn/doc.html#银罚决字〔2026〕104号")
    # 另一条当事人
    b = make_change(url="https://example.gov.cn/doc.html#银罚决字〔2026〕105号")
    # 自然键必须不同
    assert change_identity(a) != change_identity(b)


def test_change_identity_handles_missing_url() -> None:
    """验证链接为空时不会因 None 参与比较而出问题。"""
    # 无链接的变更
    a = make_change(url=None)
    # 同样无链接
    b = make_change(url=None)
    # 两者自然键相同
    assert change_identity(a) == change_identity(b)


# ============================================================
# 首次写入
# ============================================================

def test_first_write_creates_file_with_all_changes(tmp_path: Path) -> None:
    """验证文件不存在时正常创建，统计数字自洽。"""
    # 写入
    outcome = write_change_set(make_change_set([make_change(), make_change(url="https://example.gov.cn/b.html")]), tmp_path)
    # 文件存在
    assert outcome.path == tmp_path / "2026-10-05.json"
    assert outcome.path.exists()
    # 统计正确
    assert outcome.total == 2
    assert outcome.added == 2
    assert outcome.replaced == 0
    assert outcome.kept_from_previous == 0
    # 无旧文件可读问题
    assert outcome.previous_unreadable is None
    # 文件内容与统计一致
    assert read_document(outcome.path)["change_count"] == 2


# ============================================================
# 合并：核心行为
# ============================================================

def test_second_write_keeps_entries_from_the_first(tmp_path: Path) -> None:
    """**本文件最重要的一条**：单源运行不得抹掉当天其他源的发现。

    复现的是真实场景：

        全量抓取 → 文件里有 A、B 两个源的发现
        只重试 B 源 → 文件里必须仍有 A、B 两个源

    在合并实现之前，第二次写入会把文件整份重写为只剩 B 的结果。
    这条测试就是那个缺陷的反向测试。
    """
    # 第一次：模拟全量抓取，两个源的发现
    first = [make_change(policy_id="a-2026-one", url="https://a.cn/1.html")]
    # 写入
    write_change_set(make_change_set(first), tmp_path)
    # 第二次：只重试另一个源，产出一条完全不同的记录
    second = [make_change(policy_id="b-2026-two", url="https://b.cn/2.html")]
    # 写入
    outcome = write_change_set(make_change_set(second), tmp_path)

    # 总数必须是两条
    assert outcome.total == 2
    # 本次新增一条
    assert outcome.added == 1
    # 另一条是从旧文件保留的
    assert outcome.kept_from_previous == 1
    # 文件里两条都在
    ids = {item["policy_id"] for item in read_document(outcome.path)["changes"]}
    assert ids == {"a-2026-one", "b-2026-two"}


def test_rerun_replaces_same_entry_instead_of_duplicating(tmp_path: Path) -> None:
    """反向测试：重跑同一份数据不得让条目翻倍。

    若合并写成了「无脑追加」，同一天重跑两次会让变更数变成 2 倍、3 倍。
    数字虚高比丢数据更难发现——它看起来像「发现了更多东西」。
    """
    # 同一条变更写两次
    change = make_change()
    # 第一次
    write_change_set(make_change_set([change]), tmp_path)
    # 第二次
    outcome = write_change_set(make_change_set([change]), tmp_path)
    # 总数仍是一条
    assert outcome.total == 1
    # 没有新增，覆盖了一条
    assert outcome.added == 0
    assert outcome.replaced == 1
    # 文件里也只有一条
    assert len(read_document(outcome.path)["changes"]) == 1


def test_merged_run_replaces_its_own_entries_but_keeps_others(tmp_path: Path) -> None:
    """验证「覆盖自己的、保留别人的」这两件事同时成立。

    这是合并规则最容易写错的地方：只做覆盖会丢别人的条目，
    只做保留会让自己的条目重复。必须两者都对。
    """
    # 初始文件：A 源两条、B 源一条
    initial = [
        make_change(policy_id="a-1", url="https://a.cn/1.html"),
        make_change(policy_id="a-2", url="https://a.cn/2.html"),
        make_change(policy_id="b-1", url="https://b.cn/1.html"),
    ]
    # 写入
    write_change_set(make_change_set(initial), tmp_path)
    # 重跑 A 源，只产出一条（a-1），a-2 不再出现
    rerun = [make_change(policy_id="a-1", url="https://a.cn/1.html", detail="说明已更新")]
    # 写入
    outcome = write_change_set(make_change_set(rerun), tmp_path)

    # 总数 = 本次 1 条 + 保留 2 条（a-2 与 b-1）
    assert outcome.total == 3
    # 保留两条
    assert outcome.kept_from_previous == 2
    # 覆盖一条
    assert outcome.replaced == 1
    # 取明细
    document = read_document(outcome.path)
    # a-1 用的是本次版本（说明已更新），不是旧版本
    a1 = next(item for item in document["changes"] if item["policy_id"] == "a-1")
    assert a1["detail"] == "说明已更新"
    # b-1 仍在
    assert any(item["policy_id"] == "b-1" for item in document["changes"])


def test_merge_false_rewrites_the_whole_file(tmp_path: Path) -> None:
    """验证 merge=False 时整份重写，供需要「清空重来」的场景使用。"""
    # 先写两条
    write_change_set(make_change_set([make_change(), make_change(url="https://a.cn/2.html")]), tmp_path)
    # 再以 merge=False 写一条
    outcome = write_change_set(make_change_set([make_change(url="https://b.cn/1.html")]), tmp_path, merge=False)
    # 只剩一条
    assert outcome.total == 1
    assert outcome.kept_from_previous == 0
    # 文件里也只有一条
    assert len(read_document(outcome.path)["changes"]) == 1


# ============================================================
# 顶层统计必须与明细自洽
# ============================================================

def test_top_level_counts_stay_consistent_after_merge(tmp_path: Path) -> None:
    """验证合并后顶层计数按明细重算，而不是沿用本次运行的计数。

    若沿用 ``change_set.to_dict()`` 的计数，文件里会出现
    「change_count: 1 而 changes 有 40 条」这种自相矛盾的内容。
    这类「文件自己前后不一致」的问题极难发现，因为没有人会去核对一个计数。
    """
    # 先写一条
    write_change_set(make_change_set([make_change()]), tmp_path)
    # 再写一条不同的
    outcome = write_change_set(
        make_change_set([make_change(policy_id="b-1", url="https://b.cn/1.html", significance="high")]),
        tmp_path,
    )
    # 读文件
    document = read_document(outcome.path)
    # 计数与明细一致
    assert document["change_count"] == len(document["changes"]) == 2
    # 高重要性计数同样按合并后的明细重算
    assert document["high_significance_count"] == 1
    # 保留数被记进文件，让使用者知道「这份文件不是一次运行的产物」
    assert document["merged_from_previous"] == 1


def test_merged_from_previous_is_absent_when_nothing_was_kept(tmp_path: Path) -> None:
    """验证没有保留任何旧条目时，不写出 ``merged_from_previous`` 键。

    变更文件是公开产物，不该为绝大多数条目塞一个恒为 0 的字段——
    那会让「这个键出现了」失去意义。保持它与 `extra` 相同的处理原则：
    有内容才写。
    """
    # 首次写入
    outcome = write_change_set(make_change_set([make_change()]), tmp_path)
    # 键不存在
    assert "merged_from_previous" not in read_document(outcome.path)


# ============================================================
# 旧文件不可用时的行为
# ============================================================

def test_corrupt_existing_file_is_reported_not_silently_ignored(tmp_path: Path) -> None:
    """反向测试：既有文件损坏时必须报出原因，而不是静默覆盖。

    静默覆盖的后果是：损坏这件事只在报告里毫无痕迹地过去，
    下一次写入就把证据抹掉了。而变更文件是产物，一个损坏的产物
    不该让整条抓取流水线失败——本次产出仍然有效。
    因此既不能抛异常，也不能不吭声：要写进 ``previous_unreadable``。
    """
    # 造一个损坏的 JSON
    path = tmp_path / "2026-10-05.json"
    # 写入半个对象
    path.write_text('{"detected_on": "2026-10-05", "changes": [', encoding="utf-8")
    # 写入本次产出
    outcome = write_change_set(make_change_set([make_change()]), tmp_path)
    # 报告了原因
    assert outcome.previous_unreadable is not None
    # 原因里点明是解析失败
    assert "JSONDecodeError" in outcome.previous_unreadable
    # 文件已被整份重写为本次产出（而不是留着坏文件）
    assert read_document(outcome.path)["change_count"] == 1


def test_non_object_top_level_is_reported(tmp_path: Path) -> None:
    """反向测试：顶层是数组等非对象结构时同样要报出原因。

    只捕获 JSONDecodeError 是不够的——``[1, 2, 3]`` 是合法 JSON，
    解析不会报错，但 ``.get()`` 会在下一步炸掉。因此类型检查不能省。
    """
    # 顶层是数组
    path = tmp_path / "2026-10-05.json"
    # 写入
    path.write_text("[1, 2, 3]", encoding="utf-8")
    # 执行
    outcome = write_change_set(make_change_set([make_change()]), tmp_path)
    # 报出了原因
    assert outcome.previous_unreadable is not None
    # 原因说明顶层结构不对
    assert "顶层结构不是对象" in outcome.previous_unreadable


def test_existing_file_with_other_date_is_not_merged(tmp_path: Path) -> None:
    """反向测试：文件里写着别的日期时不得合并。

    路径由检测日期推导，正常情况下不会出现「文件名是 A 日、内容是 B 日」。
    真出现时说明文件被手工改过或被别的程序误写，此时把两份不同日期的记录
    混在一起，比丢弃这份文件更糟——使用者会以为它们都是同一天的发现。
    """
    # 造一份内容日期不符的文件
    path = tmp_path / "2026-10-05.json"
    # 内容声称是另一天
    path.write_text(
        json.dumps({"detected_on": "2026-01-01", "changes": [{"change_type": "discovered", "policy_id": "old"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 写入本次产出
    outcome = write_change_set(make_change_set([make_change()]), tmp_path)
    # 报出了原因
    assert outcome.previous_unreadable is not None
    # 原因点明日期不符
    assert "detected_on" in outcome.previous_unreadable
    # 且没有把旧条目混进来
    assert outcome.kept_from_previous == 0
    assert read_document(outcome.path)["change_count"] == 1


def test_malformed_change_entries_are_skipped(tmp_path: Path) -> None:
    """反向测试：旧文件里形状不对的条目被跳过，不让整次写入失败。

    条目层面出现非字典值（如 null）时，``.get()`` 会抛 AttributeError。
    这类损伤只应让那一条不被保留，并且通过总数变化被看见——
    而不是让维护者拿到一个 traceback，还不知该去改哪里。
    """
    # 造一份有坏条目的文件
    path = tmp_path / "2026-10-05.json"
    # 一条正常、两条形状不对
    path.write_text(
        json.dumps(
            {
                "detected_on": "2026-10-05",
                "changes": [
                    None,
                    {"change_type": "discovered", "policy_id": "keep-me", "url": "https://k.cn/1.html"},
                    "这不是字典",
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 写入一条与旧条目不同的新记录
    outcome = write_change_set(make_change_set([make_change(policy_id="new-one")]), tmp_path)
    # 没有抛异常，且只保留了那一条合法旧条目
    assert outcome.kept_from_previous == 1
    # 总数 = 1 条本次 + 1 条保留
    assert outcome.total == 2
    # 保留的正是那条合法条目
    assert any(item["policy_id"] == "keep-me" for item in read_document(outcome.path)["changes"])


# ============================================================
# 历史变更流已发现链接（「新发现」跨天去重的依据）
# ============================================================

def make_day_document(day: str, urls: list[str]) -> str:
    """构造一份变更文件内容并序列化为 JSON 文本。"""
    # 按链接逐条生成 discovered 明细
    changes = [
        {
            "change_type": "discovered",          # 变更类型
            "policy_id": f"pending:src:{url}",    # 临时标识
            "title": "某条目",                     # 标题
            "url": url,                           # 官方链接
        }
        for url in urls
    ]
    # 序列化顶层结构
    return json.dumps({"detected_on": day, "changes": changes}, ensure_ascii=False)


def test_previously_discovered_urls_collects_links_across_days(tmp_path: Path) -> None:
    """验证跨多天的变更文件，链接被汇总为一个集合。"""
    # 两天各写一份变更文件
    (tmp_path / "2026-10-04.json").write_text(
        make_day_document("2026-10-04", ["https://a.cn/1.html"]), encoding="utf-8"
    )
    # 第二天含一个重复链接与一个新链接
    (tmp_path / "2026-10-05.json").write_text(
        make_day_document("2026-10-05", ["https://a.cn/1.html", "https://b.cn/2.html"]), encoding="utf-8"
    )
    # 汇总
    urls = load_previously_discovered_urls(tmp_path)
    # 去重后应为两个链接
    assert urls == {"https://a.cn/1.html", "https://b.cn/2.html"}


def test_previously_discovered_urls_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """验证目录不存在时返回空集合（还没有任何历史变更是合法状态）。"""
    # 指向不存在的目录
    assert load_previously_discovered_urls(tmp_path / "没有这个目录") == set()


def test_previously_discovered_urls_skips_corrupt_files(tmp_path: Path) -> None:
    """反向测试：损坏文件被跳过，其链接**不在**集合里。

    这是刻意的「响亮失败」：被跳过的条目会在下一次抓取时被重新发现，
    以新条目的姿态出现在当天变更流里，维护者能立刻看到异常。
    若把损坏文件的链接也算进集合，损坏就会被伪装成「没有变化」。
    """
    # 一份合法文件
    (tmp_path / "2026-10-04.json").write_text(
        make_day_document("2026-10-04", ["https://a.cn/1.html"]), encoding="utf-8"
    )
    # 一份损坏文件，里面有另一个链接
    (tmp_path / "2026-10-05.json").write_text("{ 这不是 JSON", encoding="utf-8")
    # 一份顶层是数组的文件
    (tmp_path / "2026-10-06.json").write_text("[1, 2, 3]", encoding="utf-8")
    # 汇总
    urls = load_previously_discovered_urls(tmp_path)
    # 只有合法文件的链接被收集
    assert urls == {"https://a.cn/1.html"}
