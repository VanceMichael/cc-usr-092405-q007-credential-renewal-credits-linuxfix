"""表结构定义（SQLAlchemy Core）。

迁移 002 用显式 DDL 建立同名表；此处的 MetaData 供服务层构造 SQL 使用。
所有日期使用 ISO 字符串（YYYY-MM-DD），时间戳使用带时区的 ISO 字符串。
"""

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

# ── 服务元数据（001 基线） ────────────────────────────────────

service_metadata = Table(
    "service_metadata",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# ── 人员与提供方 ──────────────────────────────────────────────

persons = Table(
    "persons",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("region", String, nullable=False),
    Column("created_at", String, nullable=False),
)

providers = Table(
    "providers",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("status", String, nullable=False),  # active | revoked
    Column("revoked_at", String),
    Column("revoked_reason", String),
    Column("created_at", String, nullable=False),
)

# 人员与课程提供方之间的受限关系（利益冲突核查依据）
provider_relationships = Table(
    "provider_relationships",
    metadata,
    Column("id", String, primary_key=True),
    Column("provider_id", String, ForeignKey("providers.id"), nullable=False),
    Column("person_id", String, nullable=False),
    Column("relation", String, nullable=False),
    Column("active", Integer, nullable=False, default=1),
    Column("created_at", String, nullable=False),
)

# ── 课程目录与等效规则（均版本化、不可变） ────────────────────

equivalence_groups = Table(
    "equivalence_groups",
    metadata,
    Column("id", String, primary_key=True),
    Column("code", String, nullable=False, unique=True),
    Column("title", String, nullable=False),
    Column("current_rule_id", String),  # 指向当前生效的 equivalence_rules.id
    Column("created_at", String, nullable=False),
)

equivalence_rules = Table(
    "equivalence_rules",
    metadata,
    Column("id", String, primary_key=True),
    Column("group_id", String, ForeignKey("equivalence_groups.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String, nullable=False),  # active | superseded
    Column("effective_from", String, nullable=False),
    Column("superseded_at", String),
    Column("created_at", String, nullable=False),
)

equivalence_rule_members = Table(
    "equivalence_rule_members",
    metadata,
    Column("id", String, primary_key=True),
    Column("rule_id", String, ForeignKey("equivalence_rules.id"), nullable=False),
    # 指向 course_catalog 的某个不可变版本行
    Column("course_catalog_id", String, ForeignKey("course_catalog.id"), nullable=False),
)

course_catalog = Table(
    "course_catalog",
    metadata,
    Column("id", String, primary_key=True),
    Column("course_code", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("title", String, nullable=False),
    Column("competence_scopes", Text, nullable=False),  # JSON 数组
    Column("credits", Float, nullable=False),
    Column("valid_from", String, nullable=False),
    Column("valid_until", String),  # 空表示长期有效
    Column("applicable_regions", Text, nullable=False),  # JSON 数组，[] 表示全境适用
    Column("created_at", String, nullable=False),
)

# ── 完成证明 ──────────────────────────────────────────────────

completion_evidence = Table(
    "completion_evidence",
    metadata,
    Column("id", String, primary_key=True),
    # 提供方给出的证明业务标识；同标识异文时多行共用
    Column("evidence_uid", String, nullable=False),
    Column("provider_id", String, ForeignKey("providers.id"), nullable=False),
    Column("person_id", String, nullable=False),
    Column("course_code", String, nullable=False),
    Column("completion_date", String, nullable=False),
    Column("credits_claimed", Float, nullable=False),
    Column("content_summary", Text, nullable=False),
    # 服务端对规范化内容的哈希；相同证明（含跨机构再次提交）据此沿用原记录
    Column("content_hash", String, nullable=False, index=True),
    # recorded | under_review | void
    Column("status", String, nullable=False),
    # under_review 行指向同一 evidence_uid 的首条记录
    Column("variant_of_id", String, ForeignKey("completion_evidence.id")),
    Column("duplicate_of_id", String, ForeignKey("completion_evidence.id")),
    Column("check_result", String),  # confirmed | rejected | …
    Column("void_reason", String),
    Column("first_recorded_at", String, nullable=False),
    Column("resolved_at", String),
)

# 持久化核查队列：进程中断后可恢复，审议期限不丢失
review_queue = Table(
    "review_queue",
    metadata,
    Column("id", String, primary_key=True),
    Column("evidence_id", String, ForeignKey("completion_evidence.id"), nullable=False),
    Column("reason", String, nullable=False),  # same_uid_mismatch | manual
    Column("status", String, nullable=False),  # queued | resolved
    Column("due_at", String, nullable=False),
    Column("resolution", String),  # confirmed | rejected
    Column("notes", Text),
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", String, nullable=False),
    Column("resolved_at", String),
)

# ── 续期阈值 ──────────────────────────────────────────────────

renewal_requirements = Table(
    "renewal_requirements",
    metadata,
    Column("id", String, primary_key=True),
    Column("competence", String, nullable=False, unique=True),
    Column("required_credits", Float, nullable=False),
    Column("period_years", Integer, nullable=False),
    Column("per_cycle_cap", Float),  # 周期上限，空表示不封顶
    Column("updated_at", String, nullable=False),
)

# ── 续期申请、版本快照与意见 ──────────────────────────────────

applications = Table(
    "applications",
    metadata,
    Column("id", String, primary_key=True),
    Column("applicant_id", String, ForeignKey("persons.id"), nullable=False),
    Column("competence", String, nullable=False),
    Column("region", String, nullable=False),
    Column("period_start", String, nullable=False),
    Column("period_end", String, nullable=False),
    # 阈值快照
    Column("required_credits", Float, nullable=False),
    Column("per_cycle_cap", Float),
    Column("period_years", Integer, nullable=False),
    Column("current_version", Integer, nullable=False),
    # pending | approved | rejected
    Column("status", String, nullable=False),
    Column("decision", String),  # approved | rejected
    Column("decided_at", String),
    Column("created_at", String, nullable=False),
)

application_versions = Table(
    "application_versions",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    Column("version", Integer, nullable=False),
    # active | obsolete（新版本产生后旧版本即刻失效）
    Column("status", String, nullable=False),
    Column("trigger", String, nullable=False),  # init | reevaluation
    Column("change_summary", Text),
    # 快照时固定的等效规则版本 id 列表（JSON）
    Column("pinned_rule_ids", Text, nullable=False),
    Column("credits_accepted", Float, nullable=False),
    Column("credits_required_met", Integer, nullable=False),
    Column("created_at", String, nullable=False),
)

application_evidence_snapshots = Table(
    "application_evidence_snapshots",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_version_id", String, ForeignKey("application_versions.id"), nullable=False),
    Column("evidence_id", String, nullable=False),
    Column("order_index", Integer, nullable=False),
    # 凭证与目录快照字段
    Column("provider_id", String, nullable=False),
    Column("provider_name", String, nullable=False),
    Column("provider_status", String, nullable=False),
    Column("course_catalog_id", String),
    Column("course_code", String, nullable=False),
    Column("course_version", Integer),
    Column("course_title", String),
    Column("competence_scopes", Text),  # JSON
    Column("applicable_regions", Text),  # JSON
    Column("credits", Float, nullable=False),
    Column("credits_applied", Float),  # 计入周期上限的实际学分（可能因封顶部分计入）
    Column("completion_date", String, nullable=False),
    Column("evidence_status", String, nullable=False),
    Column("equivalence_rule_id", String),
    Column("dedup_fingerprint", String, nullable=False),
    # 逐项裁决
    Column("decision", String, nullable=False),  # included | excluded
    Column("exclusion_category", String),  # duplicate | capped | expired | out_of_scope | under_review | void | provider_revoked
    Column("exclusion_reason", Text),
)

review_assignments = Table(
    "review_assignments",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    # direct（直属审核人：专业范围） | compliance（独立合规人：利益冲突）
    Column("role", String, nullable=False),
    Column("person_id", String, nullable=False),
    Column("assigned_at", String, nullable=False),
)

review_opinions = Table(
    "review_opinions",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    Column("application_version_id", String, ForeignKey("application_versions.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("role", String, nullable=False),
    Column("reviewer_id", String, nullable=False),
    Column("decision", String, nullable=False),  # approved | rejected
    Column("comment", Text),
    Column("created_at", String, nullable=False),
)

# ── 生效后续期的风险留存 ──────────────────────────────────────

risk_annotations = Table(
    "risk_annotations",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    Column("trigger_type", String, nullable=False),  # provider_revoked | evidence_voided | equivalence_reversion
    Column("ref_id", String, nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

# ── 重评（崩溃可恢复） ────────────────────────────────────────

reevaluation_jobs = Table(
    "reevaluation_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    Column("event_type", String, nullable=False),
    Column("ref_id", String, nullable=False),
    Column("detail", Text),
    # queued | processing | done | failed
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_owner", String),
    Column("leased_at", String),
    Column("result", Text),
    Column("created_at", String, nullable=False),
    Column("processed_at", String),
)

reevaluation_logs = Table(
    "reevaluation_logs",
    metadata,
    Column("id", String, primary_key=True),
    Column("application_id", String, ForeignKey("applications.id"), nullable=False),
    Column("event_type", String, nullable=False),
    Column("ref_id", String, nullable=False),
    Column("from_version", Integer, nullable=False),
    Column("to_version", Integer, nullable=False),
    Column("changed", Integer, nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
