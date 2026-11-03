from depfix.embed import HashEmbedder
from depfix.index import Index
from depfix.retrieve import LAYERS, evaluate, gold_ids, retrieve, rrf, symbol_of
from tests.helpers import RULES, fixture_index


def ev(rule_id: str, **kw):
    idx, emb = fixture_index()
    rule = RULES.by_id[rule_id]
    return retrieve(idx, emb, rule, rule.qualified_name.split(".")[-1], **kw), idx, rule


def test_the_anchor_layer_puts_the_rules_own_page_entry_first():
    got, _, _ = ev("STD-0003")
    assert "anchor" in got[0].layers and "logging.warn(" in got[0].text
    assert got[0].url == "https://docs.python.org/3/library/logging.html"


def test_evidence_carries_its_licence_and_section():
    got, _, _ = ev("NP-0001")
    assert got[0].license.startswith("MIT") and "Main namespace" in got[0].section


def test_each_layer_alone_finds_the_entry():
    for layer in LAYERS:
        got, idx, rule = ev("NP-0006", layers=(layer,), k=4)
        assert any("np.product" in e.text for e in got), layer


def test_layers_are_reported_per_chunk():
    got, _, _ = ev("NP-0001")
    assert set(got[0].layers) >= {"anchor", "keyword"}


def test_k_limits_the_number_of_excerpts():
    got, _, _ = ev("NP-0001", k=2)
    assert len(got) == 2


def test_the_character_budget_protects_small_context_windows():
    got, _, _ = ev("NP-0001", k=4, budget_chars=300)
    assert sum(len(e.text) for e in got) <= 300


def test_an_empty_index_returns_nothing_instead_of_failing():
    rule = RULES.by_id["NP-0001"]
    assert retrieve(Index(":memory:"), HashEmbedder(), rule, "np.float_") == []


def test_a_rule_whose_page_is_not_indexed_still_gets_keyword_and_vector_hits():
    idx, emb = fixture_index()
    rule = RULES.by_id["NP-0001"].model_copy(
        update={
            "reference": RULES.by_id["NP-0001"].reference.model_copy(
                update={"url": "https://other.example/x"}
            )
        }
    )
    got = retrieve(idx, emb, rule, "np.float_")
    assert got and not any("anchor" in e.layers for e in got)


def test_rank_fusion_rewards_agreement():
    assert set(rrf(["a", "b", "c"], ["c", "b", "d"])[:2]) == {"b", "c"}  # both lists agree on them


def test_symbol_and_gold_answer_key():
    idx, _ = fixture_index()
    assert symbol_of(RULES.by_id["STD-0001"]) == "utcnow"
    gold = gold_ids(idx, RULES.by_id["STD-0001"])
    assert gold and all("utcnow" in idx.get(g).text for g in gold)  # type: ignore[union-attr]


def test_evaluation_scores_keyword_and_vector_layers_without_the_answer_key():
    idx, emb = fixture_index()
    rules = list(RULES.rules)
    kw = evaluate(idx, emb, rules, 4, ("keyword",))
    both = evaluate(idx, emb, rules, 4, ("keyword", "vector"))
    assert kw["rules"] == 21 and kw["hit@4"] == 1.0
    assert both["hit@4"] == 1.0 and 0 < both["mrr"] <= 1
    # the experiment must run for both chunkers (E1)
    idx2, emb2 = fixture_index("fixed")
    assert evaluate(idx2, emb2, rules, 4, ("keyword", "vector"))["rules"] > 0


def test_symbols_match_whole_names_only():
    from depfix.retrieve import mentions

    assert mentions("logging.warn(msg)", "warn") and not mentions("logging.warning(msg)", "warn")
    assert mentions("np.product", "product") and not mentions("np.cumproduct", "product")
    assert mentions("np.round_ here", "round_") and not mentions("np.round here", "round_")
    assert mentions("np.Inf", "Inf") and not mentions("np.inf", "Inf")
