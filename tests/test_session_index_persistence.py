"""R61 durable index persistence gate backed by the R62B candidate cases."""

# These are explicit re-exports (rather than duplication) so the required
# two-file pre-deploy command exercises the same hermetic fixtures once.
from tests.test_durable_sidebar_counts import (  # noqa: F401
    index_file,
    isolate_sidebar_store,
    session_dir,
    state_db,
    test_bounded_scanner_fails_closed_when_array_close_is_unread,
    test_compact_prefers_loaded_nonempty_sidecar_over_zero_metadata,
    test_compact_retains_positive_metadata_and_keeps_new_sessions_empty,
    test_concurrent_incremental_index_writers_never_tear_or_lose_rows,
    test_eight_mib_boundary_after_complete_element_does_not_overcount,
    test_full_rebuild_repairs_unusable_metadata_without_zeroing_nonempty_row,
    test_genuine_empty_sidecar_without_count_scans_to_zero,
    test_load_repairs_bad_legacy_metadata_from_nonempty_sidecar,
    test_metadata_only_absent_count_scans_real_sidecar_without_full_load,
    test_metadata_only_malformed_count_scans_real_sidecar_without_db,
    test_metadata_only_zero_header_is_repaired_by_structural_sidecar_count,
    test_ordinary_save_never_persists_zero_for_nonempty_sidecar,
    test_scanner_keeps_exact_count_when_only_trailing_metadata_is_truncated,
    test_scanner_rejects_malformed_first_value_and_duplicate_messages_member,
    test_unusable_sidecar_header_outranks_stale_index_with_structural_count,
)
