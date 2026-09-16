"""
Page-graph node identity (spec §11).

The graph's whole value is that it tracks WHICH PAGE the agent is on, in a
world where a multi-step application can keep one URL for all five steps and
a single step can have three different URLs. So node identity has to come
from the page's STRUCTURE — `PageSignature`'s controls, heading and step
indicator — and not from its URL.

Two properties, pulling in opposite directions, both asserted here:

  * same URL, different step (different visible controls) -> DIFFERENT nodes;
  * different URL, same observable page                   -> the SAME node.

The second is the one that was broken: `_signature_key` hashed the raw URL,
so a redirect or a query-parameter change that altered nothing observable
minted a brand-new node for a page nothing had changed about.
"""

import pytest

from automation.agents.autonomous.page_graph import (
    PageGraph,
    PageGraphEdge,
    PageGraphNode,
    _signature_key,
    _stable_url,
)
from automation.applications.page_navigator import PageSignature


def _sig(**overrides):
    base = dict(
        url="https://boards.example/acme/apply",
        title="Apply to Acme",
        heading="Personal details",
        step_indicator="Step 1 of 3",
        controls=("input:first name", "input:last name", "input:email"),
    )
    base.update(overrides)
    return PageSignature(**base)


# --- 1. same URL, different structure -> different nodes -------------------

def test_two_steps_of_an_spa_form_on_one_url_are_two_nodes():
    """The canonical multi-step SPA: the URL never changes, but step 2 shows
    entirely different controls. Keying on URL alone would collapse an entire
    application into a single node."""
    step_one = _sig()
    step_two = _sig(
        heading="Work history",
        step_indicator="Step 2 of 3",
        controls=("input:employer", "input:job title", "input:start date"),
    )
    assert step_one.url == step_two.url

    graph = PageGraph()
    graph.visit(step_one, page_type="APPLICATION_FORM")
    graph.record_action("click", "Next", succeeded=True)
    graph.visit(step_two, page_type="APPLICATION_FORM")

    assert len(graph.as_dict()["nodes"]) == 2
    assert _signature_key(step_one) != _signature_key(step_two)


def test_a_step_that_differs_only_by_its_controls_is_still_a_separate_node():
    """No heading change, no step indicator, same URL — only the fields moved.
    The controls alone must be enough to separate the nodes."""
    first = _sig(heading="", step_indicator="")
    second = _sig(heading="", step_indicator="", controls=("input:employer", "input:job title"))
    assert _signature_key(first) != _signature_key(second)


def test_a_conditional_follow_up_question_makes_it_a_new_node():
    """Answering "do you need sponsorship?" reveals a follow-up field. The
    page observably changed, so the graph must record arriving somewhere new
    rather than silently overwriting the previous visit."""
    before = _sig(controls=("select:do you require sponsorship",))
    after = _sig(controls=("select:do you require sponsorship", "input:which visa do you hold"))

    graph = PageGraph()
    graph.visit(before)
    graph.visit(after)
    assert len(graph.as_dict()["nodes"]) == 2


def test_a_step_indicator_change_alone_separates_nodes():
    assert _signature_key(_sig(step_indicator="Step 1 of 3")) != _signature_key(
        _sig(step_indicator="Step 2 of 3")
    )


# --- 2. different URL, same observable page -> same node -------------------

@pytest.mark.parametrize("variant_url", [
    "https://boards.example/acme/apply?utm_source=linkedin",
    "https://boards.example/acme/apply?gh_src=abc123&utm_campaign=q3",
    "https://boards.example/acme/apply#section-2",
    "https://boards.example/acme/apply?state=xyz#top",
])
def test_a_url_change_that_alters_nothing_observable_is_the_same_node(variant_url):
    """REGRESSION. A redirect back from an identity provider, a tracking
    parameter, or a fragment router changes the URL without changing the
    form. That is not a new page, and minting a node for it both inflated the
    graph and made `is_cycle` blind to the revisit."""
    original = _sig()
    redirected = _sig(url=variant_url)

    assert _signature_key(original) == _signature_key(redirected)

    graph = PageGraph()
    graph.visit(original)
    graph.visit(redirected)
    nodes = graph.as_dict()["nodes"]
    assert len(nodes) == 1
    assert next(iter(nodes.values()))["visits"] == 2


def test_such_a_revisit_is_correctly_reported_as_a_cycle():
    """`is_cycle` is the graph's "have we been here before" answer, used as
    corroborating evidence for NO_PROGRESS. A query-string-only change must
    not let a genuinely looping page hide from it."""
    graph = PageGraph()
    graph.visit(_sig())
    assert graph.is_cycle(_sig(url="https://boards.example/acme/apply?retry=1")) is True


def test_a_genuinely_different_path_is_still_a_different_node():
    """The converse, so the normalization cannot be "fixed" by merging
    everything: only the query/fragment is dropped, never the path."""
    assert _signature_key(_sig()) != _signature_key(
        _sig(url="https://boards.example/acme/apply/review")
    )
    assert _signature_key(_sig()) != _signature_key(
        _sig(url="https://boards.example/other-co/apply")
    )


@pytest.mark.parametrize("url,expected", [
    ("https://x.example/apply", "https://x.example/apply"),
    ("https://x.example/apply?a=1", "https://x.example/apply"),
    ("https://x.example/apply#frag", "https://x.example/apply"),
    ("https://x.example/apply?a=1#frag", "https://x.example/apply"),
    ("https://x.example/apply#frag?a=1", "https://x.example/apply"),
    ("", ""),
])
def test_stable_url_drops_only_the_query_and_fragment(url, expected):
    assert _stable_url(url) == expected


# --- 3. the loop's own comparison is deliberately NOT relaxed --------------

def test_the_stalled_page_comparison_still_counts_a_bare_url_change():
    """`PageSignature.differs_from` answers a different question — "did
    anything at all happen since the last iteration?" — for `loop.py`'s
    stalled-page escalation and `actions.py`'s staleness gate. A URL change
    IS evidence that something happened there, so relaxing the graph's node
    key must not have relaxed that."""
    assert _sig(url="https://boards.example/acme/apply?step=2").differs_from(_sig()) is True


def test_graph_identity_and_stall_detection_disagree_on_purpose():
    """Stated as one assertion so the intentional asymmetry can't be
    "cleaned up" by accident."""
    a, b = _sig(), _sig(url="https://boards.example/acme/apply?utm=1")
    assert _signature_key(a) == _signature_key(b)      # same page
    assert a.differs_from(b) is True                   # but something did happen


# --- 4. identity survives the round trip through JSONB ---------------------

def test_node_identity_survives_persistence_and_rehydration():
    """The graph is flushed into `application_progress` and read back on
    resume — a rehydrated node must still be recognized as the same page."""
    graph = PageGraph()
    graph.visit(_sig(), page_type="APPLICATION_FORM", application_stage="PERSONAL_INFORMATION")
    graph.record_action("fill", "Email", succeeded=True)
    graph.visit(_sig(url="https://boards.example/acme/apply?saved=1"))

    restored = PageGraph.from_dict(graph.as_dict())
    assert restored.is_cycle(_sig()) is True
    assert len(restored.as_dict()["nodes"]) == 1
    assert restored.as_dict()["nodes"][_signature_key(_sig())]["visits"] == 2


def test_from_dict_tolerates_an_empty_or_missing_graph():
    assert PageGraph.from_dict(None).as_dict()["nodes"] == {}
    assert PageGraph.from_dict({}).as_dict()["nodes"] == {}


# --- 5. edges record the real shape of navigation, cycles included --------

def test_a_back_navigation_shows_up_as_a_real_cycle_not_an_overwrite():
    """Revisiting an earlier page must add a visit and an edge, never replace
    the node — otherwise a loop is invisible in the recorded graph."""
    step_one = _sig()
    step_two = _sig(heading="Work history", step_indicator="Step 2 of 3",
                    controls=("input:employer",))

    # The real sequence `loop.py::_record_page_visit` performs: visit, then
    # close the edge the PREVIOUS action opened.
    graph = PageGraph()
    graph.close_pending_edge(graph.visit(step_one))
    graph.record_action("click", "Next", succeeded=True)
    graph.close_pending_edge(graph.visit(step_two))
    graph.record_action("click", "Back", succeeded=True)
    graph.close_pending_edge(graph.visit(step_one))

    data = graph.as_dict()
    assert len(data["nodes"]) == 2
    assert data["nodes"][_signature_key(step_one)]["visits"] == 2
    assert [e["action_detail"] for e in data["edges"]] == ["Next", "Back"]
    assert data["edges"][-1]["to"] == _signature_key(step_one)
    assert data["current"] == _signature_key(step_one)


def test_a_node_records_the_page_type_it_was_first_classified_as():
    graph = PageGraph()
    fingerprint = graph.visit(_sig(), page_type="APPLICATION_FORM",
                              application_stage="PERSONAL_INFORMATION")
    node = graph.as_dict()["nodes"][fingerprint]
    assert node["page_type"] == "APPLICATION_FORM"
    assert node["application_stage"] == "PERSONAL_INFORMATION"
    assert node["url"] == _sig().url


def test_the_node_and_edge_records_are_json_serializable():
    """They are written into a JSONB column, so `as_dict()` must not leak a
    dataclass or a tuple-keyed dict."""
    import json

    graph = PageGraph()
    graph.visit(_sig())
    graph.record_action("click", "Next", succeeded=False)
    graph.visit(_sig(heading="Work history", controls=("input:employer",)))
    json.dumps(graph.as_dict())  # must not raise

    assert isinstance(PageGraphNode(fingerprint="f", page_type="X", url="u").as_dict(), dict)
    assert isinstance(PageGraphEdge("a", "b", "click", "Next", True).as_dict(), dict)
