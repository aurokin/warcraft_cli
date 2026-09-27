from __future__ import annotations

from warcraft_content.search import ArticleMatchWeights, normalize_query, score_article_match, tokenize_query


def test_tokenize_query_lowercases_keeps_plus_and_dedupes_in_order() -> None:
    assert tokenize_query("Mythic+ Dungeon mythic+ Guide!") == ("mythic+", "dungeon", "guide")


def test_tokenize_query_drops_stop_words() -> None:
    assert tokenize_query("Icy Veins Balance Druid guide", stop_words={"icy", "veins", "guide"}) == ("balance", "druid")


def test_normalize_query_strips_whole_words_only_and_falls_back_to_raw_query() -> None:
    assert normalize_query("Method Guide Restoration Druid", strip_terms=("method", "guide", "guides")) == "restoration druid"
    assert normalize_query("Methodical", strip_terms=("method",)) == "methodical"
    assert normalize_query("guide", strip_terms=("guide",)) == "guide"


def test_score_article_match_reports_reasons_for_each_matched_rule() -> None:
    score, reasons = score_article_match("balance druid", "balance druid")
    assert score == 40 + 15 + 10 + 16
    assert reasons == ["exact_name", "name_prefix", "name_contains_query", "all_terms_match"]

    score, reasons = score_article_match("druid balance", "balance druid guide")
    assert score == 16
    assert reasons == ["all_terms_match"]

    assert score_article_match("", "anything") == (0, [])


def test_score_article_match_honours_custom_weights() -> None:
    weights = ArticleMatchWeights(all_terms=8)
    score, reasons = score_article_match("druid balance", "balance druid guide", weights=weights)
    assert score == 8
    assert reasons == ["all_terms_match"]
