from app.filters import (
    hf1_length,
    hf2_no_stack,
    hf4_question_form,
    hf6_short_reply,
    run_hard_filters,
)


def test_hf1_length_rejects_too_short_text():
    assert hf1_length("короткий текст") == "HF-1_length"


def test_hf1_length_accepts_normal_text():
    assert hf1_length("x" * 200) is None


def test_hf1_length_rejects_too_long_text():
    assert hf1_length("x" * 8001) == "HF-1_length"


def test_hf2_no_stack_rejects_empty_match():
    assert hf2_no_stack([]) == "HF-2_no_stack"


def test_hf2_no_stack_accepts_present_match():
    assert hf2_no_stack(["python"]) is None


def test_hf4_question_form_rejects_short_question():
    text = "кто-нибудь знает открытые вакансии python"
    assert hf4_question_form(text) == "HF-4_question"


def test_hf4_question_form_ignores_long_text_even_if_phrased_as_question():
    text = "кто-нибудь знает " + "x" * 200
    assert hf4_question_form(text) is None


def test_hf4_question_form_accepts_plain_statement():
    text = "ищем python разработчика в команду"
    assert hf4_question_form(text) is None


def test_hf6_short_reply_rejects_short_reply():
    assert hf6_short_reply("короткий ответ", reply_to_id=123) == "HF-6_short_reply"


def test_hf6_short_reply_accepts_when_not_a_reply():
    assert hf6_short_reply("короткий ответ", reply_to_id=None) is None


def test_hf6_short_reply_accepts_long_reply():
    text = "x" * 250
    assert hf6_short_reply(text, reply_to_id=123) is None


def test_run_hard_filters_passes_clean_vacancy_text():
    text = "ищем python разработчика в команду. пишите в лс если подходит стек. " * 4
    result = run_hard_filters(
        text_norm=text,
        stack_matched=["python"],
        reply_to_id=None,
        closed_re=None,
        anti_re=None,
        hiring_re=None,
    )
    assert result is None


def test_run_hard_filters_stops_at_first_failing_check():
    text = "ищем разработчика в команду. пишите в лс если подходит стек. " * 4
    result = run_hard_filters(
        text_norm=text,
        stack_matched=[],
        reply_to_id=None,
        closed_re=None,
        anti_re=None,
        hiring_re=None,
    )
    assert result == "HF-2_no_stack"
