from app.scoring import RulesCache, score_message


def _neutral_struct_features(length: int = 200) -> dict:
    return {"length": length, "bullet_line_count": 0, "hashtag_count": 0}


def test_score_message_stack_only_match():
    rules = RulesCache(stack_weights={"python": 5})
    rules.compile()

    result = score_message(
        text_norm="ищем python разработчика в команду",
        struct_features=_neutral_struct_features(),
        reply_to_id=None,
        source_kind="channel",
        source_priority=0,
        author_reputation_bonus=0,
        rules=rules,
    )

    assert result.score == 5
    assert result.stack_matched == ["python"]
    assert result.signals == {"stack": 5}
    assert result.contact is None
    assert result.compensation is None


def test_score_message_combines_signals_and_applies_hiring_cap():
    rules = RulesCache(
        stack_weights={"python": 5},
        hiring_weights={"ищем": 10},
        hiring_cap=8,
    )
    rules.compile()

    text = "ищем python разработчика. требования: опыт от 2 лет. контакт @hr_manager. зарплата 300000 руб"
    result = score_message(
        text_norm=text,
        struct_features=_neutral_struct_features(),
        reply_to_id=None,
        source_kind="job_board",
        source_priority=2,
        author_reputation_bonus=1,
        rules=rules,
    )

    # hiring weight of 10 must be clipped to hiring_cap=8, not summed raw
    assert result.signals["hiring"] == 8
    assert result.signals["has_sections"] == 3
    assert result.signals["has_contact"] == 2
    assert result.signals["has_money_pattern"] == 2
    assert result.signals["source_job_board"] == 3
    assert result.signals["source_priority"] == 2
    assert result.signals["author_reputation"] == 1
    assert result.contact == "@hr_manager"
    assert result.compensation == "300000 руб"
    assert result.score == 26


def test_score_message_applies_noise_penalty_floor():
    rules = RulesCache(
        noise_weights={"резюме": -5, "ищу работу": -6},
        noise_cap=-8,
    )
    rules.compile()

    result = score_message(
        text_norm="ищу работу резюме прикладываю опыт 3 года",
        struct_features=_neutral_struct_features(),
        reply_to_id=None,
        source_kind="channel",
        source_priority=0,
        author_reputation_bonus=0,
        rules=rules,
    )

    # raw penalty is -11, must be floored at noise_cap=-8
    assert result.signals["noise"] == -8
    assert result.score == -8
