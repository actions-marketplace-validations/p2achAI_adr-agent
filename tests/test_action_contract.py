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
    prepare_base = _step("Prepare direct publish base")
    direct = _step("Publish index directly")
    create_pr = _step("Create PR if changes")

    assert "direct" in validation["run"]
    assert "index" in validation["run"]
    assert "inputs.publish_mode == 'direct'" in prepare_base["if"]
    assert prepare_base["env"]["ADR2_GITHUB_TOKEN"] == "${{ inputs.github_token || github.token }}"
    assert "http.https://github.com/.extraheader" in prepare_base["run"]
    assert 'git checkout --detach "origin/$ADR2_PR_BASE"' in prepare_base["run"]
    assert "inputs.publish_mode == 'direct'" in direct["if"]
    assert "inputs.operation == 'index'" in direct["if"]
    assert 'push origin "HEAD:${ADR2_PR_BASE}"' in direct["run"]
    assert "inputs.publish_mode == 'pull-request'" in create_pr["if"]


def test_direct_publish_owns_git_auth_for_every_remote_operation():
    prepare_base = _step("Prepare direct publish base")["run"]
    direct = _step("Publish index directly")["run"]

    assert 'git -c "http.https://github.com/.extraheader="' in prepare_base
    assert 'git -c "http.https://github.com/.extraheader="' in direct
    assert 'git_with_auth fetch origin "$ADR2_PR_BASE"' in prepare_base
    assert 'git_with_auth fetch origin "$ADR2_PR_BASE"' in direct
    assert 'git_with_auth push origin "HEAD:${ADR2_PR_BASE}"' in direct


def test_validation_mode_generates_without_staging_or_publishing():
    stage = _step("Stage changed docs paths")
    create_pr = _step("Create PR if changes")

    assert "inputs.publish_mode != 'none'" in stage["if"]
    assert "inputs.publish_mode == 'pull-request'" in create_pr["if"]
