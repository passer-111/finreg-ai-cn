"""变更检测 —— 对比两次抓取结果，产出「这周变了什么」。

为什么这是本项目的核心输出
--------------------------
竞品普遍只回答「库里有什么」，而合规团队真正需要的是「**最近变了什么**」。
一份 2023 年就存在的政策，对今天的合规工作没有增量信息；
真正需要行动的是昨天刚发布或刚被修订的那一条。

参考竞品 GRCX 与 compliance-radar 的做法：
- GRCX 用「Monitor → Analyse → Record」三步流程
- compliance-radar 用 SHA-256 哈希做零误报的内容变更检测

本模块结合两者思路，但做了三件它们没做的事：

1. **区分「新增/内容变更/元数据变更」三种变更类型。**
   只有内容变更才需要重新做合规影响评估，元数据变更（如补了个标签）不需要。
   不区分会让变更流充满噪声，最终无人阅读。

2. **哈希只看归一化后的正文，不看整页 HTML。**
   整页哈希会因访问计数器、时间戳等动态元素产生大量误报。

3. **输出人类可读的摘要。** 变更流是给人看的，不是给程序看的。
"""

# 导入 dataclass 用于定义变更记录
from dataclasses import dataclass, field
# 导入 date 与 datetime 用于时间处理
from datetime import date
# 导入 Iterable 类型标注。从 collections.abc 导入的原因同 cli.py：
# typing 中的容器别名已弃用，标准库抽象基类才是正确来源。
from collections.abc import Iterable
# 导入 Any 类型标注
from typing import Any

# 从本包导入数据模型
from finreg_ai.models import Policy

# 从抓取器包导入原始条目类型
from finreg_ai.fetchers.base import RawDoc, now_china_iso


# ============================================================
# 变更记录
# ============================================================

# 变更类型常量。用常量而非裸字符串，避免拼写错误导致下游筛不到数据。
CHANGE_TYPE_NEW = "new"                      # 首次收录
CHANGE_TYPE_CONTENT = "content_changed"      # 正文实质变更（需重新做合规评估）
CHANGE_TYPE_METADATA = "metadata_changed"    # 仅元数据变更（不需重新评估）
CHANGE_TYPE_STATUS = "status_changed"        # 法律状态变更（最需要关注）
CHANGE_TYPE_DISCOVERED = "discovered"        # 发现新条目但尚未完成结构化录入


@dataclass
class Change:
    """一条变更记录。

    字段设计的目标是让合规人员扫一眼就知道「要不要现在处理」。
    因此 ``significance`` 字段是必需的——把所有变更按同等重要性平铺，
    等于没有优先级。
    """

    # 变更类型，取值见上面的 CHANGE_TYPE_* 常量
    change_type: str
    # 受影响的政策 id
    policy_id: str
    # 政策标题，便于人在变更流中快速识别
    title: str
    # 变更发生日期
    detected_on: date
    # 变更说明，人类可读
    detail: str
    # 重要性等级：high / medium / low。用于变更流排序
    significance: str = "medium"
    # 官方链接，便于一键回源核对
    url: str | None = None
    # 涉及的具体字段（元数据变更时记录改了哪些字段）
    fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        # 逐字段输出，日期转 ISO 字符串
        return {
            "change_type": self.change_type,                    # 变更类型
            "policy_id": self.policy_id,                        # 政策标识
            "title": self.title,                                # 政策标题
            "detected_on": self.detected_on.isoformat(),        # 检测日期
            "significance": self.significance,                  # 重要性
            "detail": self.detail,                              # 变更说明
            "url": self.url,                                    # 官方链接
            "fields": self.fields,                              # 涉及字段
        }


@dataclass
class ChangeSet:
    """一次检测得到的全部变更，也是 data/changes/YYYY-MM-DD.json 的内容。"""

    # 检测日期
    detected_on: date
    # 变更列表
    changes: list[Change] = field(default_factory=list)
    # 检测时间戳（ISO 8601 带时区）
    generated_at: str = field(default_factory=now_china_iso)
    # 参与本次检测的政策总数，用于观察覆盖率
    policies_scanned: int = 0
    # 抓取失败的源列表，显式记录以避免「无变更」被误读为「一切正常」
    failed_sources: list[str] = field(default_factory=list)

    @property
    def high_significance(self) -> list[Change]:
        """筛出高重要性变更，用于生成告警。"""
        # 按重要性字段过滤
        return [c for c in self.changes if c.significance == "high"]

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        # 构造输出结构
        return {
            "detected_on": self.detected_on.isoformat(),        # 检测日期
            "generated_at": self.generated_at,                  # 生成时间
            "policies_scanned": self.policies_scanned,          # 扫描政策数
            "change_count": len(self.changes),                  # 变更总数
            "high_significance_count": len(self.high_significance),  # 高重要性变更数
            "failed_sources": self.failed_sources,              # 抓取失败的源
            "changes": [c.to_dict() for c in self.changes],     # 变更明细
        }


# ============================================================
# 变更检测
# ============================================================

def detect_policy_changes(old: dict[str, Policy], new: dict[str, Policy], today: date) -> list[Change]:
    """对比两次政策集合，产出变更列表。

    检测三类变更：
    - 新增：``new`` 中有而 ``old`` 中没有
    - 状态变更：同一条记录的 ``status`` 发生变化（最高优先级）
    - 元数据变更：其他字段变化（内容哈希、生效日期、关键义务等）

    参数 today 由调用方传入，便于测试注入固定日期。
    """
    # 结果容器
    changes: list[Change] = []

    # --- 第一类：新增记录 ---
    for pid, policy in new.items():
        # 仅处理新增项
        if pid in old:
            # 已存在，跳过
            continue
        # 判断重要性：现行有效且与 AI 核心相关的政策优先级最高
        significance = "high" if policy.status.is_currently_valid and policy.ai_relevance.value == "core" else "medium"
        # 追加变更记录
        changes.append(
            Change(
                change_type=CHANGE_TYPE_NEW,                                        # 类型：新增
                policy_id=pid,                                                      # 政策标识
                title=policy.title,                                                 # 标题
                detected_on=today,                                                  # 检测日期
                significance=significance,                                          # 重要性
                detail=f"新增收录：{policy.issuer}《{policy.title}》（状态：{policy.status.value}）",  # 说明
                url=policy.source.url,                                              # 官方链接
            )
        )

    # --- 第二、三类：已有记录的变化 ---
    for pid, new_policy in new.items():
        # 跳过新增项（已在上面处理）
        old_policy = old.get(pid)
        # 不存在旧记录则跳过
        if old_policy is None:
            # 继续下一条
            continue

        # 先检查法律状态变化——这是合规场景中最需要立即知晓的变更
        if old_policy.status != new_policy.status:
            # 状态变为失效类时重要性为高（合规义务解除，但可能有遗留义务）
            # 状态变为有效类时同样是高（新增合规义务）
            changes.append(
                Change(
                    change_type=CHANGE_TYPE_STATUS,                                 # 类型：状态变更
                    policy_id=pid,                                                  # 政策标识
                    title=new_policy.title,                                         # 标题
                    detected_on=today,                                              # 检测日期
                    significance="high",                                            # 状态变更一律高优先级
                    detail=f"法律状态变更：{old_policy.status.value} → {new_policy.status.value}",  # 说明
                    url=new_policy.source.url,                                      # 官方链接
                    fields=["status"],                                              # 涉及字段
                )
            )

        # 再检查其它元数据变化
        changed_fields: list[str] = []
        # 检查内容哈希变化——这是「官方在未发通知的情况下修改了文件」的信号
        old_hash = old_policy.source.content_hash
        # 取出新哈希
        new_hash = new_policy.source.content_hash
        # 两者都存在且不同，说明正文变了
        if old_hash and new_hash and old_hash != new_hash:
            # 记录字段变化
            changed_fields.append("content_hash")
        # 检查生效日期变化（政策延期生效是常见情况）
        if old_policy.effective_from != new_policy.effective_from:
            # 记录
            changed_fields.append("effective_from")
        # 检查失效日期变化
        if old_policy.effective_until != new_policy.effective_until:
            # 记录
            changed_fields.append("effective_until")
        # 检查取代关系变化（版本链更新是最重要的元数据变更之一）
        if old_policy.superseded_by != new_policy.superseded_by:
            # 记录
            changed_fields.append("superseded_by")
        # 检查关键义务条目数变化
        if len(old_policy.key_obligations) != len(new_policy.key_obligations):
            # 记录
            changed_fields.append("key_obligations")
        # 检查最近核验日期变化（说明有人做了维护工作）
        if old_policy.last_verified != new_policy.last_verified:
            # 记录
            changed_fields.append("last_verified")

        # 有字段变化时生成一条变更记录
        if changed_fields:
            # 正文哈希变化属高重要性——可能意味着监管在无通知情况下改动了规则
            is_content_change = "content_hash" in changed_fields
            # 追加变更记录
            changes.append(
                Change(
                    # 正文变了记为 content_changed，否则记为 metadata_changed
                    change_type=CHANGE_TYPE_CONTENT if is_content_change else CHANGE_TYPE_METADATA,
                    policy_id=pid,                                                  # 政策标识
                    title=new_policy.title,                                         # 标题
                    detected_on=today,                                              # 检测日期
                    # 正文变更重要性更高
                    significance="high" if is_content_change else "low",
                    # 说明变化的字段
                    detail=f"字段变更：{', '.join(changed_fields)}",                 # 说明
                    url=new_policy.source.url,                                      # 官方链接
                    fields=changed_fields,                                          # 涉及字段
                )
            )

    # --- 额外检查：记录陈旧告警 ---
    # 这条不是「政策变更」，而是「记录维护滞后」——但同样需要进入变更流，
    # 否则陈旧会永远无人处理（这正是竞品 delschlangen/ai-legislation-tracker 的失败方式）
    for pid, policy in new.items():
        # 计算距上次核验的天数
        days = policy.days_since_verified(today)
        # 超过 180 天的记录单独告警
        if days > 180:
            # 追加告警
            changes.append(
                Change(
                    change_type=CHANGE_TYPE_METADATA,                               # 归入元数据类
                    policy_id=pid,                                                  # 政策标识
                    title=policy.title,                                             # 标题
                    detected_on=today,                                              # 检测日期
                    significance="medium",                                          # 中优先级
                    detail=f"记录陈旧：距上次核验已 {days} 天，建议重新核对官方页面确认状态",  # 说明
                    url=policy.source.url,                                          # 官方链接
                    fields=["last_verified"],                                       # 涉及字段
                )
            )

    # 按重要性排序，让高优先级变更排在前面
    order = {"high": 0, "medium": 1, "low": 2}
    # 稳定排序，保持同优先级内的原始顺序
    return sorted(changes, key=lambda c: order.get(c.significance, 9))


def diff_discovered(docs: Iterable[RawDoc], known_urls: set[str], today: date) -> list[Change]:
    """把新抓到的原始条目中尚未录入的，转成「已发现待录入」变更。

    这是流水线闭环的关键一环。抓取器只能发现条目，无法自动完成结构化录入
    （因为「文件层级」「约束力」「关键义务」必须人工判断，
    机器只能给出线索，不能替人定性）。

    因此设计上把「发现」与「录入」分离：
    - 流水线每日自动发现新条目，写入变更流
    - 人工根据变更流决定哪些需要正式录入为政策记录

    这样做的代价是需要人工介入，但换来的是数据的可信度——
    全自动生成的结构化字段在合规场景中不可接受。
    """
    # 结果容器
    changes: list[Change] = []
    # 逐条检查
    for doc in docs:
        # 已录入过的链接跳过
        if doc.url in known_urls:
            # 继续下一条
            continue
        # 追加「已发现待录入」变更
        changes.append(
            Change(
                change_type=CHANGE_TYPE_DISCOVERED,                                  # 类型：发现新条目
                policy_id=f"pending:{doc.source_id}:{doc.url}",                      # 尚无正式 id，用来源加链接临时标识
                title=doc.title,                                                     # 标题
                detected_on=today,                                                   # 检测日期
                significance="medium",                                               # 中优先级，等待人工判断
                detail=f"发现新条目待录入：{doc.title}（来源：{doc.source_id}）",     # 说明
                url=doc.url,                                                         # 官方链接
            )
        )
    # 返回结果
    return changes
