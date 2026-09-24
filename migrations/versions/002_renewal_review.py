"""年度续期学分审议：目录、证明、核查、申请快照、双岗审核、重评。"""

from alembic import op
import sqlalchemy as sa

revision = "002_renewal_review"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persons",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String()),
        sa.Column("revoked_reason", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "provider_relationships",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("provider_id", sa.String(), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("relation", sa.String(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "course_catalog",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("course_code", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("competence_scopes", sa.Text(), nullable=False),
        sa.Column("credits", sa.Float(), nullable=False),
        sa.Column("valid_from", sa.String(), nullable=False),
        sa.Column("valid_until", sa.String()),
        sa.Column("applicable_regions", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "equivalence_groups",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("current_rule_id", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "equivalence_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.String(), sa.ForeignKey("equivalence_groups.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("effective_from", sa.String(), nullable=False),
        sa.Column("superseded_at", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "equivalence_rule_members",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("rule_id", sa.String(), sa.ForeignKey("equivalence_rules.id"), nullable=False),
        sa.Column("course_catalog_id", sa.String(), sa.ForeignKey("course_catalog.id"), nullable=False),
    )
    op.create_table(
        "completion_evidence",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("evidence_uid", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("course_code", sa.String(), nullable=False),
        sa.Column("completion_date", sa.String(), nullable=False),
        sa.Column("credits_claimed", sa.Float(), nullable=False),
        sa.Column("content_summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False, index=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("variant_of_id", sa.String(), sa.ForeignKey("completion_evidence.id")),
        sa.Column("duplicate_of_id", sa.String(), sa.ForeignKey("completion_evidence.id")),
        sa.Column("check_result", sa.String()),
        sa.Column("void_reason", sa.String()),
        sa.Column("first_recorded_at", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.String()),
    )
    op.create_table(
        "review_queue",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("evidence_id", sa.String(), sa.ForeignKey("completion_evidence.id"), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("due_at", sa.String(), nullable=False),
        sa.Column("resolution", sa.String()),
        sa.Column("notes", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.String()),
    )
    op.create_table(
        "renewal_requirements",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("competence", sa.String(), nullable=False, unique=True),
        sa.Column("required_credits", sa.Float(), nullable=False),
        sa.Column("period_years", sa.Integer(), nullable=False),
        sa.Column("per_cycle_cap", sa.Float()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("applicant_id", sa.String(), sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("competence", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column("period_start", sa.String(), nullable=False),
        sa.Column("period_end", sa.String(), nullable=False),
        sa.Column("required_credits", sa.Float(), nullable=False),
        sa.Column("per_cycle_cap", sa.Float()),
        sa.Column("period_years", sa.Integer(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decision", sa.String()),
        sa.Column("decided_at", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "application_versions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("change_summary", sa.Text()),
        sa.Column("pinned_rule_ids", sa.Text(), nullable=False),
        sa.Column("credits_accepted", sa.Float(), nullable=False),
        sa.Column("credits_required_met", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "application_evidence_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_version_id", sa.String(), sa.ForeignKey("application_versions.id"), nullable=False),
        sa.Column("evidence_id", sa.String(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("provider_name", sa.String(), nullable=False),
        sa.Column("provider_status", sa.String(), nullable=False),
        sa.Column("course_catalog_id", sa.String()),
        sa.Column("course_code", sa.String(), nullable=False),
        sa.Column("course_version", sa.Integer()),
        sa.Column("course_title", sa.String()),
        sa.Column("competence_scopes", sa.Text()),
        sa.Column("applicable_regions", sa.Text()),
        sa.Column("credits", sa.Float(), nullable=False),
        sa.Column("credits_applied", sa.Float()),
        sa.Column("completion_date", sa.String(), nullable=False),
        sa.Column("evidence_status", sa.String(), nullable=False),
        sa.Column("equivalence_rule_id", sa.String()),
        sa.Column("dedup_fingerprint", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("exclusion_category", sa.String()),
        sa.Column("exclusion_reason", sa.Text()),
    )
    op.create_table(
        "review_assignments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("assigned_at", sa.String(), nullable=False),
    )
    op.create_table(
        "review_opinions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("application_version_id", sa.String(), sa.ForeignKey("application_versions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("reviewer_id", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("comment", sa.Text()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "risk_annotations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("trigger_type", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "reevaluation_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("detail", sa.Text()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String()),
        sa.Column("leased_at", sa.String()),
        sa.Column("result", sa.Text()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("processed_at", sa.String()),
    )
    op.create_table(
        "reevaluation_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("application_id", sa.String(), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("changed", sa.Integer(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "reevaluation_logs",
        "reevaluation_jobs",
        "risk_annotations",
        "review_opinions",
        "review_assignments",
        "application_evidence_snapshots",
        "application_versions",
        "applications",
        "renewal_requirements",
        "review_queue",
        "completion_evidence",
        "course_catalog",
        "equivalence_rule_members",
        "equivalence_rules",
        "equivalence_groups",
        "provider_relationships",
        "providers",
        "persons",
    ):
        op.drop_table(table)
