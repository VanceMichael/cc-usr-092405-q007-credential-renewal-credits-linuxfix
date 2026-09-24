"""关系表结构定义。

迁移脚本与本模块共用同一份 MetaData，避免结构漂移。
日期一律存 ISO 字符串（``YYYY-MM-DD`` 或带时间的 UTC ISO），JSON 载荷存 TEXT。
"""

from sqlalchemy import Column, Float, ForeignKey, Index, Integer, MetaData, String, Table, Text

metadata = MetaData()

# ---------------------------------------------------------------- 基础主数据

experts = Table(
    "experts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("expert_code", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("home_region", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)

credentials = Table(
    "credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("credential_code", String(64), nullable=False, unique=True),
    Column("expert_id", Integer, ForeignKey("experts.id"), nullable=False),
    # JSON 数组：该凭证要求的能力范围
    Column("required_scopes", Text, nullable=False),
    Column("cycle_years", Integer, nullable=False, default=3),
    Column("required_credits", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
)

providers = Table(
    "providers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("provider_code", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    # active / revoked
    Column("status", String(16), nullable=False, default="active"),
    Column("revoked_at", String(40), nullable=True),
    Column("created_at", String(40), nullable=False),
)

reviewers = Table(
    "reviewers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reviewer_code", String(64), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    # supervisor（直属审核人，看专业范围）/ compliance（独立合规人，看利益冲突）
    Column("role", String(16), nullable=False),
    Column("created_at", String(40), nullable=False),
)

# 审核人与课程提供方之间的受限关系（利益冲突来源）
reviewer_restrictions = Table(
    "reviewer_restrictions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reviewer_id", Integer, ForeignKey("reviewers.id"), nullable=False),
    Column("provider_id", Integer, ForeignKey("providers.id"), nullable=False),
    Column("reason", String(256), nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
)

# ---------------------------------------------------------------- 课程目录

courses = Table(
    "courses",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("course_code", String(64), nullable=False, unique=True),
    Column("provider_id", Integer, ForeignKey("providers.id"), nullable=False),
    Column("title", String(256), nullable=False),
    Column("credits", Float, nullable=False),
    # JSON 数组：课程覆盖的能力范围
    Column("scopes", Text, nullable=False),
    Column("valid_from", String(10), nullable=False),
    Column("valid_to", String(10), nullable=True),
    # JSON 数组：课程直接适用的地区
    Column("regions", Text, nullable=False),
    Column("equivalence_group", String(64), nullable=True),
    Column("created_at", String(40), nullable=False),
)

# 等效组规则版本：换版只影响尚未生效的申请
equivalence_group_versions = Table(
    "equivalence_group_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("group_code", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    # JSON 数组：该版本下互认的地区
    Column("regions", Text, nullable=False),
    Column("effective_from", String(10), nullable=False),
    Column("created_at", String(40), nullable=False),
)

# 周期学分上限：scope 或等效组在滚动周期内可计入学分上限
period_caps = Table(
    "period_caps",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cap_key", String(64), nullable=False, unique=True),
    # scope / equivalence_group
    Column("cap_type", String(32), nullable=False),
    Column("max_credits", Float, nullable=False),
    Column("created_at", String(40), nullable=False),
)

# ---------------------------------------------------------------- 完成证明

# 受理后的证明记录：同一 (proof_key) 首次受理一行，重复提交沿用
proofs = Table(
    "proofs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("proof_key", String(128), nullable=False, unique=True),
    Column("provider_id", Integer, ForeignKey("providers.id"), nullable=False),
    Column("expert_id", Integer, ForeignKey("experts.id"), nullable=False),
    Column("course_id", Integer, ForeignKey("courses.id"), nullable=False),
    Column("completed_at", String(10), nullable=False),
    Column("content_hash", String(64), nullable=False),
    # accepted / suspended / voided
    Column("status", String(16), nullable=False, default="accepted"),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

# 每一次上传的内容版本：相同内容沿用原记录，异文进入核查
proof_submissions = Table(
    "proof_submissions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("proof_id", Integer, ForeignKey("proofs.id"), nullable=False),
    Column("provider_id", Integer, ForeignKey("providers.id"), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("payload", Text, nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("created_at", String(40), nullable=False),
)

# 核查队列（持久化，系统中断不丢）
verification_cases = Table(
    "verification_cases",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("proof_id", Integer, ForeignKey("proofs.id"), nullable=False),
    Column("reason", String(64), nullable=False),
    # open / resolved
    Column("status", String(16), nullable=False, default="open"),
    Column("due_at", String(40), nullable=False),
    Column("escalated", Integer, nullable=False, default=0),
    Column("resolution", String(32), nullable=True),
    Column("resolved_at", String(40), nullable=True),
    Column("created_at", String(40), nullable=False),
)

# ---------------------------------------------------------------- 续期申请与审议

renewal_applications = Table(
    "renewal_applications",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_code", String(64), nullable=False, unique=True),
    Column("credential_id", Integer, ForeignKey("credentials.id"), nullable=False),
    Column("expert_id", Integer, ForeignKey("experts.id"), nullable=False),
    # in_review / approved / rejected
    Column("status", String(16), nullable=False, default="in_review"),
    # 快照版本：发起为 1，重评变化时 +1；意见只能落在各自看到的版本上
    Column("snapshot_version", Integer, nullable=False, default=1),
    Column("window_end", String(10), nullable=False),
    Column("window_start", String(10), nullable=False),
    Column("review_due_at", String(40), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
)

# 申请快照分项：逐项记录采用或排除的来龙去脉
application_items = Table(
    "application_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("renewal_applications.id"), nullable=False),
    Column("snapshot_version", Integer, nullable=False),
    Column("proof_id", Integer, ForeignKey("proofs.id"), nullable=False),
    Column("submission_id", Integer, ForeignKey("proof_submissions.id"), nullable=False),
    Column("course_code", String(64), nullable=False),
    Column("provider_code", String(64), nullable=False),
    Column("completed_at", String(10), nullable=False),
    Column("proof_credits", Float, nullable=False),
    Column("credits_counted", Float, nullable=False),
    # counted / capped / excluded
    Column("decision", String(16), nullable=False),
    # 排除或封顶原因：duplicate / cap / expired / scope / region / provider_revoked / proof_voided / proof_suspended
    Column("reasons", Text, nullable=False),
    # 完整判定轨迹（JSON），抽查时直接看到来龙去脉
    Column("trace", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    Index("ix_application_items_app", "application_id", "snapshot_version"),
)

review_assignments = Table(
    "review_assignments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("renewal_applications.id"), nullable=False),
    Column("reviewer_id", Integer, ForeignKey("reviewers.id"), nullable=False),
    Column("role", String(16), nullable=False),
    Column("created_at", String(40), nullable=False),
)

review_opinions = Table(
    "review_opinions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("renewal_applications.id"), nullable=False),
    Column("reviewer_id", Integer, ForeignKey("reviewers.id"), nullable=False),
    Column("role", String(16), nullable=False),
    # 意见落在审核人当时看到的快照版本上
    Column("snapshot_version", Integer, nullable=False),
    Column("decision", String(16), nullable=False),
    Column("comment", Text, nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
)

# 已生效续期：证据冻结，外部接口只回答指定日期是否有效
effective_renewals = Table(
    "effective_renewals",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("renewal_applications.id"), nullable=False, unique=True),
    Column("credential_id", Integer, ForeignKey("credentials.id"), nullable=False),
    Column("expert_id", Integer, ForeignKey("experts.id"), nullable=False),
    Column("effective_from", String(10), nullable=False),
    Column("valid_until", String(10), nullable=False),
    Column("snapshot_version", Integer, nullable=False),
    Column("created_at", String(40), nullable=False),
)

# 生效后发生的风险（撤销/作废/换版）只追加标注，不改写原证据
risk_annotations = Table(
    "risk_annotations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("effective_renewal_id", Integer, ForeignKey("effective_renewals.id"), nullable=False),
    # provider_revoked / proof_voided / equivalence_version
    Column("kind", String(32), nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)

# ---------------------------------------------------------------- 持久作业与事件

# 持久作业队列：审议期限、核查 SLA、重评、等效版本激活；中断恢复后继续
durable_jobs = Table(
    "durable_jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_type", String(48), nullable=False),
    Column("payload", Text, nullable=False),
    Column("run_at", String(40), nullable=False),
    # pending / running / done / failed
    Column("status", String(16), nullable=False, default="pending"),
    Column("attempts", Integer, nullable=False, default=0),
    Column("locked_at", String(40), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Index("ix_durable_jobs_due", "status", "run_at"),
)

# 申请事件日志：审议过程可追溯
application_events = Table(
    "application_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("application_id", Integer, ForeignKey("renewal_applications.id"), nullable=False),
    Column("event_type", String(48), nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)
