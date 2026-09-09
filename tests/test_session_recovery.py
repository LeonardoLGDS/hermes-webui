"""R61 durable recovery gate backed by the R62B candidate cases."""

from tests.test_durable_sidebar_counts import (  # noqa: F401
    index_file,
    isolate_sidebar_store,
    session_dir,
    state_db,
    test_api_session_pipeline_shows_repaired_row_and_hides_genuine_empty,
    test_backfill_preserves_exact_partial_sidecar_and_db_tail_identity,
    test_legacy_backfill_reconciles_permitted_db_tail_before_write,
    test_materializer_projects_tool_and_mixed_rows_without_raw_db_count,
    test_missing_index_recovery_materializes_db_tail_before_bulk_write,
    test_missing_index_recovery_preserves_exact_partial_transcript_order,
    test_partially_trimmed_sidecar_replays_tail_but_watermark_blocks_resurrect,
    test_recovery_count_never_drifts_down_to_a_smaller_db_snapshot,
    test_recovery_preserves_archived_title_and_approval_runtime_state,
    test_restart_boundary_does_not_drift_down_after_two_materializations,
    test_scanner_rejects_malformed_first_value_and_duplicate_messages_member,
    test_state_db_projection_filters_inactive_and_preserves_tool_identity,
    test_title_only_non_untitled_session_remains_visible_across_backfill,
    test_truncation_watermark_blocks_replay_and_remains_sidecar_authoritative,
)
