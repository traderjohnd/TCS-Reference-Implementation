"""
Commit 5/6 — web-evidence-v1 contract: canonicalization, ordering,
deduplication, digest stability/sensitivity, bounded summary.
"""

from __future__ import annotations

import pytest

from tcs.providers.web_evidence import (
    Citation,
    ConsultedSource,
    MAX_CITED_TEXT_CHARS,
    SearchAction,
    WebRetrievalEvidence,
    bounded_summary,
    canonical_evidence_bytes,
    canonicalize_url,
    compute_retrieval_status,
    evidence_dict_digest,
    evidence_digest,
    query_hash,
)


def _evidence(**overrides):
    ev = WebRetrievalEvidence(
        provider="openai", model="gpt-4o",
        retrieval_started_at="2026-07-30T12:00:00Z",
        retrieval_completed_at="2026-07-30T12:00:05Z",
        provider_request_id="resp_1",
    )
    ev.search_actions = [SearchAction(
        ordinal=0, provider_call_id="ws_1", query="retention policy",
        status="completed",
    )]
    ev.consulted_sources = [ConsultedSource(
        first_seen_ordinal=0, display_url="https://example.com/a",
        title="A", cited=False, search_call_ids=["ws_1"],
    )]
    ev.citations = [Citation(
        ordinal=0, source_display_url="https://example.com/a",
        text_block_ordinal=0, start_offset=5, end_offset=20,
    )]
    ev.retrieval_status = "success"
    for k, v in overrides.items():
        setattr(ev, k, v)
    return ev


class TestCanonicalUrl:
    def test_scheme_host_lowercased_default_port_stripped(self):
        assert canonicalize_url("HTTPS://Example.COM:443/Path?b=2&a=1#frag") \
            == "https://example.com/Path?b=2&a=1"
        assert canonicalize_url("HTTP://host.example:80/") \
            == "http://host.example/"

    def test_empty_path_normalized_fragment_removed(self):
        assert canonicalize_url("https://example.com#top") \
            == "https://example.com/"

    def test_path_case_and_query_order_preserved(self):
        assert canonicalize_url("https://example.com/CaseSensitive?z=1&a=2") \
            == "https://example.com/CaseSensitive?z=1&a=2"

    def test_non_default_port_preserved(self):
        assert canonicalize_url("https://example.com:8443/x") \
            == "https://example.com:8443/x"

    def test_tracking_params_not_removed(self):
        assert canonicalize_url("https://e.com/p?utm_source=x") \
            == "https://e.com/p?utm_source=x"

    @pytest.mark.parametrize("bad", [
        None, "", "javascript:alert(1)", "ftp://example.com/x",
        "data:text/html;base64,xxx", "not a url",
    ])
    def test_non_http_rejected(self, bad):
        assert canonicalize_url(bad) is None

    def test_distinct_resources_never_merged(self):
        a = canonicalize_url("https://e.com/p?a=1&b=2")
        b = canonicalize_url("https://e.com/p?b=2&a=1")  # order differs
        assert a != b  # query ordering preserved — no over-normalization


class TestOrderingAndDedup:
    def test_source_dedup_merges_cited_and_linkage(self):
        ev = _evidence()
        ev.consulted_sources = [
            ConsultedSource(first_seen_ordinal=2,
                            display_url="https://Example.com/a",
                            title="A-late", cited=True,
                            search_call_ids=["ws_2"]),
            ConsultedSource(first_seen_ordinal=0,
                            display_url="https://example.com/a",
                            title="A", cited=False,
                            search_call_ids=["ws_1"]),
            ConsultedSource(first_seen_ordinal=1,
                            display_url="https://example.com/b",
                            title="B", cited=False),
        ]
        ev.citations = []
        ev.finalize()
        assert len(ev.consulted_sources) == 2
        merged = ev.consulted_sources[0]
        assert merged.first_seen_ordinal == 0     # earliest wins
        assert merged.cited is True               # logical OR
        assert merged.search_call_ids == ["ws_1", "ws_2"]  # deterministic
        assert merged.title == "A"                # no concatenation

    def test_citation_marks_source_cited(self):
        ev = _evidence()
        ev.consulted_sources[0].cited = False
        ev.finalize()
        assert ev.consulted_sources[0].cited is True

    def test_deterministic_ordering(self):
        ev = _evidence()
        ev.search_actions = [
            SearchAction(ordinal=1, query="b", status="completed"),
            SearchAction(ordinal=0, query="a", status="completed"),
        ]
        ev.citations = [
            Citation(ordinal=1, source_display_url="https://e.com/x",
                     text_block_ordinal=1, start_offset=None),
            Citation(ordinal=0, source_display_url="https://e.com/x",
                     text_block_ordinal=0, start_offset=50),
            Citation(ordinal=2, source_display_url="https://e.com/x",
                     text_block_ordinal=0, start_offset=10),
        ]
        ev.finalize()
        assert [a.ordinal for a in ev.search_actions] == [0, 1]
        assert [(c.text_block_ordinal, c.start_offset)
                for c in ev.citations] == [(0, 10), (0, 50), (1, None)]

    def test_source_order_variation_normalizes_to_same_digest(self):
        ev1, ev2 = _evidence(), _evidence()
        extra = ConsultedSource(first_seen_ordinal=1,
                                display_url="https://example.com/b",
                                title="B")
        ev1.consulted_sources = [ev1.consulted_sources[0], extra]
        ev2.consulted_sources = [
            ConsultedSource(first_seen_ordinal=1,
                            display_url="https://example.com/b",
                            title="B"),
            ev2.consulted_sources[0],
        ]
        assert evidence_digest(ev1) == evidence_digest(ev2)


class TestDigest:
    def test_identical_evidence_same_digest(self):
        assert evidence_digest(_evidence()) == evidence_digest(_evidence())

    @pytest.mark.parametrize("mutate", [
        lambda ev: setattr(ev.consulted_sources[0], "display_url",
                           "https://example.com/CHANGED"),
        lambda ev: setattr(ev.search_actions[0], "query", "changed query"),
        lambda ev: ev.citations.append(Citation(
            ordinal=1, source_display_url="https://example.com/a",
            text_block_ordinal=0)),
        lambda ev: setattr(ev, "error_summary", "search_failed"),
        lambda ev: setattr(ev, "retrieval_status", "partial"),
    ])
    def test_digest_sensitivity(self, mutate):
        base = evidence_digest(_evidence())
        changed = _evidence()
        mutate(changed)
        assert evidence_digest(changed) != base

    def test_dict_digest_round_trip(self):
        ev = _evidence()
        digest = evidence_digest(ev)
        assert evidence_dict_digest(ev.to_dict()) == digest

    def test_query_hash_never_invented(self):
        assert query_hash(None) is None
        assert query_hash("") is None
        assert len(query_hash("q")) == 64

    def test_missing_fields_explicit_not_omitted(self):
        d = _evidence().to_dict()
        src = d["consulted_sources"][0]
        for key in ("provider_source_id", "page_age"):
            assert key in src and src[key] is None
        cit = d["citations"][0]
        assert "cited_text" in cit and cit["cited_text"] is None
        assert "error_summary" in d and d["error_summary"] is None

    def test_cited_text_bounded(self):
        c = Citation(ordinal=0, source_display_url="https://e.com/x",
                     cited_text="x" * (MAX_CITED_TEXT_CHARS + 500))
        assert len(c.cited_text) == MAX_CITED_TEXT_CHARS

    def test_canonical_bytes_ascii_and_sorted(self):
        raw = canonical_evidence_bytes(_evidence())
        assert raw == raw.decode("ascii").encode("ascii")


class TestBoundedSummary:
    def test_summary_shape_and_no_payloads(self):
        ev = _evidence()
        s = bounded_summary(ev, "webq-1")
        assert s == {
            "schema_version": "web-evidence-v1",
            "retrieval_mode": "live_web",
            "provider": "openai",
            "model": "gpt-4o",
            "search_call_count": 1,
            "consulted_source_count": 1,
            "cited_source_count": 1,
            "retrieval_status": "success",
            "web_evidence_digest": evidence_digest(ev),
            "evidence_artifact_id": "webq-1",
        }
        assert "consulted_sources" not in s and "citations" not in s


class TestRetrievalStatus:
    def _ev(self, actions=1, ok_actions=None, sources=1, citations=1):
        ev = WebRetrievalEvidence(provider="p", model="m")
        ok = actions if ok_actions is None else ok_actions
        ev.search_actions = [
            SearchAction(ordinal=i,
                         status="completed" if i < ok else "error")
            for i in range(actions)
        ]
        ev.consulted_sources = [
            ConsultedSource(first_seen_ordinal=i,
                            display_url=f"https://e.com/{i}")
            for i in range(sources)
        ]
        ev.citations = [
            Citation(ordinal=i, source_display_url="https://e.com/0")
            for i in range(citations)
        ]
        return ev

    def test_statuses(self):
        assert compute_retrieval_status(
            self._ev(actions=0, sources=0, citations=0), "text") == \
            "retrieval_not_performed"
        assert compute_retrieval_status(
            self._ev(ok_actions=0, sources=0, citations=0), "text") == \
            "retrieval_error"
        assert compute_retrieval_status(
            self._ev(sources=0, citations=0), "text") == "no_results"
        assert compute_retrieval_status(self._ev(), "") == "empty_output"
        assert compute_retrieval_status(
            self._ev(citations=0), "text") == "no_citations"
        assert compute_retrieval_status(
            self._ev(actions=2, ok_actions=1), "text") == "partial"
        assert compute_retrieval_status(self._ev(), "text") == "success"
        assert compute_retrieval_status(self._ev(), "text", paused=True) == \
            "paused"
