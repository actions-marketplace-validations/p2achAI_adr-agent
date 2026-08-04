from pathlib import Path

import yaml


ACTION_PATH = Path(__file__).resolve().parents[1] / "action.yml"


def _action():
    return yaml.safe_load(ACTION_PATH.read_text(encoding="utf-8"))


def _step(name):
    return next(step for step in _action()["runs"]["steps"] if step.get("name") == name)


def test_publish_mode_defaults_to_pull_request():
    publish_mode = _action()["inputs"]["publish_mode"]

    assert publish_mode["default"] == "pull-request"


def test_direct_publish_is_index_only_and_does_not_create_pr():
    validation = _step("Validate publish mode")
    direct = _step("Publish index directly")
    create_pr = _step("Create PR if changes")

    assert "direct" in validation["run"]
    assert "index" in validation["run"]
    assert "inputs.publish_mode == 'direct'" in direct["if"]
    assert "inputs.operation == 'index'" in direct["if"]
    assert 'push origin "HEAD:${ADR2_PR_BASE}"' in direct["run"]
    assert "inputs.publish_mode == 'pull-request'" in create_pr["if"]


def test_validation_mode_generates_without_staging_or_publishing():
    stage = _step("Stage changed docs paths")
    create_pr = _step("Create PR if changes")

    assert "inputs.publish_mode != 'none'" in stage["if"]
    assert "inputs.publish_mode == 'pull-request'" in create_pr["if"]
