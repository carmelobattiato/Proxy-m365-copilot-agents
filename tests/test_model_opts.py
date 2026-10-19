"""Tests for model name option parsing."""

from __future__ import annotations

from lmgateway.model_opts import ModelOptions, model_names, parse_model_options


def test_bare_model_name():
    opts = parse_model_options("lm-gateway")
    assert opts == ModelOptions()


def test_persist_token():
    opts = parse_model_options("lm-gateway:persist")
    assert opts.persist is True
    assert opts.fresh is False
    assert opts.tone is None


def test_fresh_token():
    opts = parse_model_options("lm-gateway:fresh")
    assert opts.fresh is True
    assert opts.persist is False


def test_tone_alias_sonnet55():
    opts = parse_model_options("lm-gateway:sonnet5.5")
    assert opts.tone == "Claude_Sonnet"


def test_tone_alias_sonnet5():
    opts = parse_model_options("lm-gateway:sonnet5")
    assert opts.tone == "Claude_Sonnet_5"


def test_tone_alias_auto():
    opts = parse_model_options("lm-gateway:auto")
    assert opts.tone == "Magic"


def test_combined_tone_and_persist():
    opts = parse_model_options("lm-gateway:sonnet5:persist")
    assert opts.tone == "Claude_Sonnet_5"
    assert opts.persist is True


def test_combined_tone_and_fresh():
    opts = parse_model_options("lm-gateway:sonnet5.5:fresh")
    assert opts.tone == "Claude_Sonnet"
    assert opts.fresh is True


def test_unknown_tokens_are_ignored():
    opts = parse_model_options("lm-gateway:unknown:xyz")
    assert opts == ModelOptions()


def test_model_names_includes_all_aliases():
    names = model_names("lm-gateway")
    assert names[0] == "lm-gateway"
    assert "lm-gateway:auto" in names
    assert "lm-gateway:sonnet5.5" in names
    assert "lm-gateway:sonnet5" in names