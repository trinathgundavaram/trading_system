import yaml

from config_loader import save_config_preserving_comments

SRC = """# header comment
watchlist:
- NBIS   # keep this
- HCA
risk:
  # why this limit exists
  max_dd: 15.0   # trailing note
  kill_switch_triggered: false
other:
  keep: ${SOME_VAR:-x}
"""


def test_changes_apply_and_comments_survive(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SRC)
    raw = yaml.safe_load(SRC)
    raw["risk"]["kill_switch_triggered"] = True
    raw["watchlist"] = ["NBIS"]
    save_config_preserving_comments(p, raw)
    out = p.read_text()
    assert "# header comment" in out and "# why this limit exists" in out and "# trailing note" in out
    new = yaml.safe_load(out)
    assert new["risk"]["kill_switch_triggered"] is True and new["watchlist"] == ["NBIS"]
    assert new["risk"]["max_dd"] == 15.0 and new["other"]["keep"] == "${SOME_VAR:-x}"   # placeholder not expanded


def test_unchanged_dict_leaves_file_semantically_identical(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SRC)
    save_config_preserving_comments(p, yaml.safe_load(SRC))
    assert yaml.safe_load(p.read_text()) == yaml.safe_load(SRC)
    assert p.read_text().count("#") == SRC.count("#")
    assert not list(tmp_path.glob("*.tmp"))
