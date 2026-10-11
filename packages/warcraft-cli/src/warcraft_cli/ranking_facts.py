"""Provider ranking evidence the wrapper recognizes, independent of provider app imports."""

STALE_GUIDE_REASON = "stale_guide"
WIKI_QUERY_COVERAGE_REASONS = frozenset(
    {
        "exact_title",
        "exact_api_title",
        "exact_handler_title",
        "exact_event_title",
        "title_prefix",
        "title_contains_query",
        "normalized_title_match",
        "all_terms_match",
        "guide_title_terms",
        "expansion_alias_match",
    }
)
