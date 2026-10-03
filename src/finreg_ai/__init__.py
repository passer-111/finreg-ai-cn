"""finreg-ai-cn —— 中国金融领域 AI 合规政策知识库。

本包提供三件事：

1. **数据模型**（``models``）：把政策记录表示为带类型约束的 Python 对象，
   避免在代码各处用裸字典取值导致字段名拼写错误。

2. **抓取器**（``fetchers``）：从各监管机构官网抓取政策发布信息。
   每个源独立失败，单源崩溃不影响整体流水线。

3. **流水线与命令行**（``pipeline`` / ``cli``）：编排抓取 → 解析 → 比对 → 产出，
   并通过 ``finreg`` 命令对外提供入口。

设计原则（写在代码最前面，因为它们是所有取舍的依据）：

- **时效性优先于覆盖量。** 一条记录的价值不在于「我们收录了它」，
  而在于「我们准确知道它现在是否有效、被谁取代」。因此数据模型里
  时效性字段最多、最细，且校验最严。

- **LLM 不写入事实字段。** 本包不依赖任何大模型。
  合规场景下模型生成的内容不可作为事实依据，因此架构上根本不允许它进入数据层。

- **单源失效不拖垮整体。** 监管网站会改版、会超时、会下架。
  任何一处网络问题都只应影响它自己，并留下可见的失败记录。

- **离线可测。** 已实测部分监管网站在某些网络环境下不可达，
  因此流水线必须支持 fixture 驱动，CI 不依赖外网。
"""

# 包版本号，与 pyproject.toml 保持一致；单独定义便于运行期读取
__version__ = "0.1.0"

# 项目内部使用的默认编码，显式声明以避免 Windows 平台默认 cp936 导致中文乱码
DEFAULT_ENCODING = "utf-8"

# 导出核心对象，方便 `from finreg_ai import Policy` 直接使用
from finreg_ai.models import (  # noqa: E402  导入需置于常量定义之后
    AIRelevance,             # AI 相关度枚举
    Bindingness,             # 约束力性质枚举
    InstrumentType,          # 文件效力层级枚举
    Issuer,                  # 发布机构模型
    KeyObligation,           # 关键义务模型
    Policy,                  # 政策记录模型（核心）
    PolicyStatus,            # 政策法律状态枚举
    Source,                  # 数据源配置模型
    SourceRef,               # 记录中的来源溯源信息模型
    SourceTier,              # 来源权威等级枚举
    VerifiedBy,              # 核验方式枚举
    Version,                 # 政策版本模型
)

# 显式列出对外公开的符号，避免 `from finreg_ai import *` 引入内部变量
__all__ = [
    "__version__",        # 版本号
    "DEFAULT_ENCODING",   # 默认编码常量
    "AIRelevance",        # AI 相关度
    "Bindingness",        # 约束力
    "InstrumentType",     # 文件层级
    "Issuer",             # 发布机构
    "KeyObligation",      # 关键义务
    "Policy",             # 政策记录
    "PolicyStatus",       # 政策状态
    "Source",             # 数据源
    "SourceRef",          # 来源溯源
    "SourceTier",         # 来源等级
    "VerifiedBy",         # 核验方式
    "Version",            # 政策版本
]
