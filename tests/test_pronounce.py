"""A word an engine says wrong is said the config's way: the `pronounce` map, word to spoken form.

A coined room or tool name ("k8s", a digit inside a word) is read by its letters; the listener
hears the right word in replies and in speaker lines alike.
"""

from __future__ import annotations

import argparse
import json

import pytest

import claude_code_tts.config as config
import claude_code_tts.daemon as d
from claude_code_tts.cli import cmd_pronounce
from claude_code_tts.filter import pronounce

TABLE = {"kubectl": "cube control", "kube": "cube"}


class TestPronounce:
    def test_a_listed_word_is_said_its_way(self):
        assert pronounce("Fox, in room kubectl", TABLE) == "Fox, in room cube control"

    def test_the_longer_word_wins_and_the_shorter_one_is_whole_words_only(self):
        assert pronounce("kube and kubectl", TABLE) == "cube and cube control"
        assert pronounce("kubectls", TABLE) == "kubectls"

    def test_any_case_and_next_to_punctuation(self):
        assert pronounce("KUBECTL#2012, kube-proxy.", TABLE) == "cube control#2012, cube-proxy."

    def test_a_word_inside_another_is_left_alone(self):
        assert pronounce("xkube", TABLE) == "xkube"

    @pytest.mark.parametrize("table", [None, [], "kube", {}, {"kube": 3}, {"  ": "x"}])
    def test_anything_but_a_map_of_strings_changes_nothing(self, table):
        assert pronounce("kube", table) == "kube"

    def test_a_spoken_form_is_not_read_again(self):
        assert pronounce("a b", {"a": "b", "b": "c"}) == "b c"


def test_the_daemon_says_it_before_any_engine_sees_the_text(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TTS_CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"pronounce": TABLE}))
    seen: list[str] = []
    monkeypatch.setattr(d, "pronounce", lambda text, table: seen.append(pronounce(text, table)) or seen[-1])
    monkeypatch.setattr(d, "get_persona_config", lambda persona: (_ for _ in ()).throw(RuntimeError("engine")))
    with pytest.raises(RuntimeError, match="engine"):
        d._generate_speech_unleveled("Fox of House claude, in room kubectl", "p", tmp_path / "o.wav")
    assert seen == ["Fox of House claude, in room cube control"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "TTS_CONFIG_FILE", path)
    path.write_text(json.dumps({"mode": "queue"}))
    return path


def run(word=None, said=None, remove=False):
    cmd_pronounce(argparse.Namespace(word=word, said=said, remove=remove))


class TestPronounceCommand:
    def test_set_show_list_and_remove(self, cfg, capsys):
        run("kubectl", "cube control")
        assert json.loads(cfg.read_text()) == {"mode": "queue", "pronounce": {"kubectl": "cube control"}}
        run("Kubectl")
        assert "Kubectl is said: cube control" in capsys.readouterr().out
        run()
        assert "kubectl" in capsys.readouterr().out
        run("KUBECTL", remove=True)
        assert json.loads(cfg.read_text())["pronounce"] == {}

    def test_setting_again_in_another_case_replaces_it(self, cfg):
        run("kube", "koob")
        run("Kube", "cube")
        assert json.loads(cfg.read_text())["pronounce"] == {"Kube": "cube"}

    @pytest.mark.parametrize("word, said", [("two words", "x"), ("w", " "), ("w", "x" * 201)])
    def test_a_bad_entry_is_refused_and_nothing_written(self, cfg, word, said):
        with pytest.raises(SystemExit):
            run(word, said)
        assert "pronounce" not in json.loads(cfg.read_text())

    def test_removing_a_word_not_there_fails(self, cfg):
        with pytest.raises(SystemExit):
            run("nope", remove=True)
