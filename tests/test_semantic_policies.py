from app.semantic import infer_container_minimum

APPROVED=[
 {"question":"Can I purchase half container?","answer":"The answer is no we only do full containers. We don't sell half containers. We only sell full containers."},
 {"question":"What products can I buy from the store?","answer":"We sell used mixed clothing."}
]

def test_single_item_policy_entailment():
    for q in ["can I buy 1 product?","Can I purchase one item?","Do you sell single pieces?","Can we order half a container?"]:
        match=infer_container_minimum(q,APPROVED)
        assert match is not None, q
        assert "full containers" in match["answer"]

def test_no_inference_from_unrelated_questions():
    for q in ["What products do you sell?","Can I buy a full container?",
              "How many products come in a container?","Can I get a delivery estimate?",
              "Do you repair one motorcycle?"]:
        assert infer_container_minimum(q,APPROVED) is None, q

def test_no_policy_without_explicit_approved_rule():
    assert infer_container_minimum("Can I buy 1 product?",[
        {"question":"Can I purchase half container?","answer":"We have many containers in stock."}
    ]) is None
