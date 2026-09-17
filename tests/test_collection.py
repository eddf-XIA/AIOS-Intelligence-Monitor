"""Deduplication, URL identity, source scoring and article extraction."""

from __future__ import annotations

import datetime as dt

import pytest

from aios.services.article_extractor import (
    ArticleExtractor,
    canonicalize_url,
    content_hash,
    url_hash,
)
from aios.services.collector import Candidate, domain_of
from aios.services.deduplicator import (
    dedupe_candidates,
    filter_excluded,
    normalize_title,
    title_similarity,
)
from aios.services.source_scoring import ScoringContext, rank, score_candidate


def candidate(title="T", url="https://example.com/a", **kwargs):
    return Candidate(title=title, url=url, **kwargs)


class TestUrlIdentity:
    def test_tracking_params_are_stripped(self):
        a = canonicalize_url("https://www.huawei.com/news?id=7&utm_source=x&fbclid=y")
        b = canonicalize_url("https://huawei.com/news?id=7")
        assert a == b

    def test_fragment_and_trailing_slash_ignored(self):
        a = canonicalize_url("https://example.com/path/#section")
        b = canonicalize_url("https://example.com/path")
        assert a == b

    def test_same_url_hashes_equal(self):
        assert url_hash("https://www.example.com/x/") == url_hash("http://example.com/x")

    def test_different_urls_differ(self):
        assert url_hash("https://example.com/a") != url_hash("https://example.com/b")

    def test_content_hash_ignores_whitespace_and_case(self):
        assert content_hash("Hello   World\n") == content_hash("hello world")

    def test_empty_content_hash_is_blank(self):
        assert content_hash("") == ""
        assert content_hash("   ") == ""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.huawei.com/cn/news", "huawei.com"),
            ("http://developer.huawei.com/x", "developer.huawei.com"),
            ("not a url", ""),
        ],
    )
    def test_domain_of(self, url, expected):
        assert domain_of(url) == expected


class TestDeduplication:
    def test_identical_titles_collapse(self):
        items = [
            candidate("华为发布 HarmonyOS 6", "https://a.com/1"),
            candidate("华为发布 HarmonyOS 6", "https://b.com/2"),
        ]
        assert len(dedupe_candidates(items)) == 1

    def test_same_url_collapses_despite_different_titles(self):
        items = [
            candidate("Title one", "https://a.com/x?utm_source=1"),
            candidate("Completely different headline here", "https://a.com/x"),
        ]
        assert len(dedupe_candidates(items)) == 1

    def test_publisher_suffix_is_ignored(self):
        assert normalize_title("Huawei launches X - Reuters") == normalize_title(
            "Huawei launches X"
        )

    def test_distinct_stories_are_kept(self):
        items = [
            candidate("华为发布 HarmonyOS 6 系统", "https://a.com/1"),
            candidate("英伟达公布新一代互联总线规格", "https://b.com/2"),
        ]
        assert len(dedupe_candidates(items)) == 2

    def test_same_body_at_different_urls_collapses(self):
        body = "A" * 200
        items = [
            candidate("First headline", "https://a.com/1", body_text=body),
            candidate("A totally unrelated headline", "https://b.com/2", body_text=body),
        ]
        assert len(dedupe_candidates(items)) == 1

    def test_untitled_items_dropped(self):
        assert dedupe_candidates([candidate("", "https://a.com/1")]) == []

    def test_similarity_bounds(self):
        assert title_similarity("abc", "abc") == 1.0
        assert title_similarity("", "abc") == 0.0


class TestExclusions:
    def test_banned_keyword_removes_candidate(self):
        items = [
            candidate("华为招聘工程师", "https://a.com/1"),
            candidate("华为发布新系统", "https://b.com/2"),
        ]
        kept = filter_excluded(items, ["招聘"])
        assert [c.title for c in kept] == ["华为发布新系统"]

    def test_snippet_is_also_checked(self):
        items = [candidate("正常标题", "https://a.com/1", snippet="内含优惠券信息")]
        assert filter_excluded(items, ["优惠券"]) == []

    def test_no_keywords_keeps_everything(self):
        items = [candidate("x", "https://a.com/1")]
        assert len(filter_excluded(items, [])) == 1


class TestSourceScoring:
    def test_preferred_domain_outranks_generic_trust(self):
        context = ScoringContext(preferred_domains={"developer.huawei.com": 8})
        official = candidate(url="https://developer.huawei.com/a", domain="developer.huawei.com")
        generic = candidate(url="https://nvidia.com/b", domain="nvidia.com")
        assert score_candidate(official, context) > score_candidate(generic, context)

    def test_aggregators_are_penalised(self):
        aggregator = candidate(url="https://news.google.com/x", domain="news.google.com")
        plain = candidate(url="https://someblog.net/x", domain="someblog.net")
        assert score_candidate(aggregator) < score_candidate(plain)

    def test_extracted_body_increases_score(self):
        without = candidate(url="https://x.com/1", domain="x.com")
        with_body = candidate(url="https://x.com/2", domain="x.com", body_text="y" * 2000)
        assert score_candidate(with_body) > score_candidate(without)

    def test_recency_increases_score(self):
        now = dt.datetime(2026, 9, 15, 12, 0)
        fresh = candidate(url="https://x.com/1", domain="x.com", published_at=now)
        stale = candidate(
            url="https://x.com/2", domain="x.com", published_at=now - dt.timedelta(days=20)
        )
        assert score_candidate(fresh, None, now) > score_candidate(stale, None, now)

    def test_rank_sorts_best_first_and_sets_scores(self):
        items = [
            candidate(url="https://news.google.com/x", domain="news.google.com"),
            candidate(url="https://huawei.com/y", domain="huawei.com", body_text="z" * 2000),
        ]
        ordered = rank(items)
        assert ordered[0].domain == "huawei.com"
        assert all(c.trust_score != 0 for c in ordered)

    def test_scoring_is_rule_based_and_deterministic(self):
        item = candidate(url="https://huawei.com/y", domain="huawei.com")
        assert score_candidate(item) == score_candidate(item)


class TestArticleExtractor:
    def test_extracts_paragraphs_and_drops_chrome(self):
        html = """
        <html><body>
          <nav>Menu menu menu menu menu menu menu menu menu menu</nav>
          <script>var x = 1;</script>
          <p>短</p>
          <p>This is a sufficiently long paragraph of article body text to be kept.</p>
          <footer>Footer text that is long enough to matter but should be dropped.</footer>
        </body></html>
        """
        text = ArticleExtractor().extract(html)
        assert "sufficiently long paragraph" in text
        assert "Menu menu" not in text
        assert "var x" not in text
        assert "短" not in text

    def test_respects_max_chars(self):
        html = "<html><body><p>" + ("word " * 5000) + "</p></body></html>"
        assert len(ArticleExtractor(max_chars=500).extract(html)) <= 500

    def test_aggregator_urls_are_skipped(self):
        assert ArticleExtractor().fetch("https://news.google.com/rss/articles/abc") == ""

    def test_empty_input(self):
        assert ArticleExtractor().extract("") == ""

    def test_falls_back_when_no_paragraphs(self):
        html = "<html><body><div>Some text without p tags at all here.</div></body></html>"
        assert "Some text without p tags" in ArticleExtractor().extract(html)
