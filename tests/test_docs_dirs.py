import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "adr2_agent_action.py"


def load_module(monkeypatch, tmp_path, docs_dirs=None):
    anthropic_stub = types.SimpleNamespace(Anthropic=object, APIError=Exception)
    openai_stub = types.SimpleNamespace(OpenAI=object, OpenAIError=Exception)
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_stub)
    monkeypatch.setitem(sys.modules, "openai", openai_stub)
    monkeypatch.setenv("ADR2_REPO_ROOT", str(tmp_path))
    if docs_dirs is None:
        monkeypatch.delenv("ADR2_DOCS_DIRS", raising=False)
    else:
        monkeypatch.setenv("ADR2_DOCS_DIRS", docs_dirs)

    spec = importlib.util.spec_from_file_location("adr2_agent_action", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, "adr2_agent_action", module)
    spec.loader.exec_module(module)
    return module


def test_default_docs_context_points_to_root_docs(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    contexts = module.resolve_docs_contexts()

    assert len(contexts) == 1
    assert contexts[0].docs_dir == tmp_path / "docs"
    assert contexts[0].aar_dir == tmp_path / "docs" / "aar"
    assert contexts[0].adr_dir == tmp_path / "docs" / "adr"
    assert contexts[0].index_path == tmp_path / "docs" / "adr" / "index.json"


def test_docs_dirs_contexts_accept_newline_and_comma_separated_relative_paths(monkeypatch, tmp_path):
    module = load_module(
        monkeypatch,
        tmp_path,
        "apps/backend/docs\napps/frontend/docs, apps/cms-mqtt-api/docs",
    )

    contexts = module.resolve_docs_contexts()

    assert [context.docs_dir for context in contexts] == [
        tmp_path / "apps" / "backend" / "docs",
        tmp_path / "apps" / "frontend" / "docs",
        tmp_path / "apps" / "cms-mqtt-api" / "docs",
    ]
    assert contexts[0].aar_dir == tmp_path / "apps" / "backend" / "docs" / "aar"
    assert contexts[1].adr_dir == tmp_path / "apps" / "frontend" / "docs" / "adr"


def test_docs_dirs_contexts_accept_absolute_paths(monkeypatch, tmp_path):
    external_docs = tmp_path.parent / "external-docs"
    module = load_module(monkeypatch, tmp_path, str(external_docs))

    contexts = module.resolve_docs_contexts()

    assert contexts[0].docs_dir == external_docs.resolve()
    assert contexts[0].aar_dir == external_docs.resolve() / "aar"
    assert module.display_path(external_docs.resolve()) == str(external_docs.resolve())


def test_candidate_decision_requires_boolean_true(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    is_candidate, scope = module.normalize_candidate_decision(
        {"isCandidate": "false", "decisionScope": "api-contract"}
    )

    assert is_candidate is False
    assert scope == "api-contract"


def test_candidate_decision_rejects_missing_scope(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    is_candidate, scope = module.normalize_candidate_decision({"isCandidate": True})

    assert is_candidate is False
    assert scope == ""


def test_candidate_decision_rejects_minor_change(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    is_candidate, scope = module.normalize_candidate_decision(
        {"isCandidate": True, "decisionScope": "minor-change"}
    )

    assert is_candidate is False
    assert scope == "minor-change"


def test_candidate_decision_accepts_valid_candidate(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    is_candidate, scope = module.normalize_candidate_decision(
        {"isCandidate": True, "decisionScope": "api-contract"}
    )

    assert is_candidate is True
    assert scope == "api-contract"


def _write_adr(path, *, front_matter_yaml, body=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front_matter_yaml}\n---\n\n{body}", encoding="utf-8")


def test_parse_front_matter_records_failure_without_raising(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    bad_adr = tmp_path / "docs" / "adr" / "ADR-0001-broken.md"
    _write_adr(
        bad_adr,
        front_matter_yaml="id: ADR-0001\nvalidation_rules:\n- `unterminated backtick scalar",
    )

    module.PARSE_FAILURES.clear()
    meta, _ = module.parse_front_matter(bad_adr)

    assert meta == {}
    assert len(module.PARSE_FAILURES) == 1
    assert module.PARSE_FAILURES[0][0] == bad_adr


def test_catalog_existing_adrs_drops_unparseable_but_records_failure(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    contexts = module.resolve_docs_contexts()
    context = contexts[0]

    good_adr = context.adr_dir / "ADR-0002-ok.md"
    _write_adr(good_adr, front_matter_yaml="id: ADR-0002\ntitle: Ok")
    bad_adr = context.adr_dir / "ADR-0001-broken.md"
    _write_adr(
        bad_adr,
        front_matter_yaml="id: ADR-0001\nvalidation_rules:\n- `unterminated backtick scalar",
    )

    module.PARSE_FAILURES.clear()
    catalog = module.catalog_existing_adrs(context)

    assert [item["id"] for item in catalog] == ["ADR-0002"]
    assert len(module.PARSE_FAILURES) == 1


def test_main_regenerates_index_with_no_aar_candidates(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    contexts = module.resolve_docs_contexts()
    context = contexts[0]

    adr_path = context.adr_dir / "ADR-0001-existing.md"
    _write_adr(adr_path, front_matter_yaml="id: ADR-0001\ntitle: Existing ADR")

    (tmp_path / "README.md").write_text("ADR2 instructions", encoding="utf-8")

    module.main()

    assert context.index_path.exists()
    written = json.loads(context.index_path.read_text(encoding="utf-8"))
    assert written["count"] == 1
    assert written["items"][0]["id"] == "ADR-0001"


def test_main_rerun_with_no_changes_does_not_touch_index_file(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    context = module.resolve_docs_contexts()[0]

    adr_path = context.adr_dir / "ADR-0001-existing.md"
    _write_adr(adr_path, front_matter_yaml="id: ADR-0001\ntitle: Existing ADR")
    (tmp_path / "README.md").write_text("ADR2 instructions", encoding="utf-8")

    module.main()
    first_mtime = context.index_path.stat().st_mtime_ns
    first_content = context.index_path.read_bytes()

    # A second run against an unchanged corpus (the common steady state once
    # every run regenerates the index) must not rewrite the file at all --
    # otherwise every no-op CI run would still produce a spurious diff.
    module.main()

    assert context.index_path.stat().st_mtime_ns == first_mtime
    assert context.index_path.read_bytes() == first_content


def test_write_index_is_a_noop_when_items_unchanged(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]

    good_adr = context.adr_dir / "ADR-0001-existing.md"
    _write_adr(good_adr, front_matter_yaml="id: ADR-0001\ntitle: Existing ADR")
    catalog = module.catalog_existing_adrs(context)

    module.write_index(catalog, context)
    first_write = context.index_path.read_text(encoding="utf-8")
    first_generated_at = json.loads(first_write)["generated_at"]

    # Re-running with an identical catalog must not touch generated_at (or
    # the file at all) -- otherwise every no-op run would still churn a
    # timestamp-only diff.
    module.write_index(catalog, context)
    second_write = context.index_path.read_text(encoding="utf-8")

    assert second_write == first_write
    assert json.loads(second_write)["generated_at"] == first_generated_at


def test_write_index_rewrites_when_items_actually_change(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]

    good_adr = context.adr_dir / "ADR-0001-existing.md"
    _write_adr(good_adr, front_matter_yaml="id: ADR-0001\ntitle: Existing ADR")
    catalog = module.catalog_existing_adrs(context)
    module.write_index(catalog, context)
    first_generated_at = json.loads(context.index_path.read_text(encoding="utf-8"))["generated_at"]

    _write_adr(good_adr, front_matter_yaml="id: ADR-0001\ntitle: Renamed ADR")
    changed_catalog = module.catalog_existing_adrs(context)
    module.write_index(changed_catalog, context)
    written = json.loads(context.index_path.read_text(encoding="utf-8"))

    assert written["items"][0]["title"] == "Renamed ADR"
    # generated_at may or may not differ depending on clock resolution, but
    # the important behavior (rewrite happened) is covered by the title
    # assertion above; this just documents intent.
    assert isinstance(first_generated_at, str)


def test_main_raises_when_a_front_matter_parse_failure_exists(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    contexts = module.resolve_docs_contexts()
    context = contexts[0]

    bad_adr = context.adr_dir / "ADR-0001-broken.md"
    _write_adr(
        bad_adr,
        front_matter_yaml="id: ADR-0001\nvalidation_rules:\n- `unterminated backtick scalar",
    )
    (tmp_path / "README.md").write_text("ADR2 instructions", encoding="utf-8")

    with pytest.raises(RuntimeError, match="front matter parsing"):
        module.main()


def test_already_promoted_removed(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    assert not hasattr(module, "already_promoted")


def test_normalize_adr_scope_coerces_invalid_values(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    assert module.normalize_adr_scope("api") == "api"
    assert module.normalize_adr_scope("frontend/ui") == module.DEFAULT_ADR_SCOPE
    assert module.normalize_adr_scope("") == module.DEFAULT_ADR_SCOPE
    assert module.normalize_adr_scope(None) == module.DEFAULT_ADR_SCOPE


def test_index_term_key_folds_case_and_separators(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    assert module.index_term_key("measurement-v2") == module.index_term_key("Measurement V2")
    assert module.index_term_key("settopbox") == module.index_term_key("set-top-box")
    assert module.index_term_key("settopbox") == module.index_term_key("SettopBox")


def test_build_index_term_canonical_map_picks_majority_form(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    catalog = [
        {"index_terms": ["wifi", "WiFi"]},
        {"index_terms": ["wifi"]},
        {"index_terms": ["wifi", "measurement-v2"]},
        {"index_terms": ["Measurement V2"]},
    ]
    canonical_map = module.build_index_term_canonical_map(catalog)

    assert canonical_map[module.index_term_key("wifi")] == "wifi"
    assert canonical_map[module.index_term_key("measurement-v2")] == "Measurement V2"


def test_canonicalize_index_terms_aliases_known_and_passes_through_new(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    canonical_map = {module.index_term_key("wifi"): "wifi"}
    result = module.canonicalize_index_terms(["WiFi", "brand-new-term"], canonical_map)

    assert result == ["wifi", "brand-new-term"]


def test_canonicalize_index_terms_dedupes_after_aliasing(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)

    canonical_map = {module.index_term_key("wifi"): "wifi"}
    result = module.canonicalize_index_terms(["WiFi", "wifi", "WIFI"], canonical_map)

    assert result == ["wifi"]


def _write_domains_yml(context, entries):
    lines = ["domains:"]
    for entry in entries:
        lines.append(f"  - key: {entry['key']}")
        lines.append(f"    label: \"{entry.get('label', entry['key'])}\"")
        terms = entry.get("match_terms", [])
        if terms:
            lines.append("    match_terms:")
            for term in terms:
                lines.append(f"      - {term}")
    context.domains_path.parent.mkdir(parents=True, exist_ok=True)
    context.domains_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_domains_returns_empty_without_domains_yml(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]

    assert module.load_domains(context) == []


def test_load_domains_parses_curated_taxonomy(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]
    _write_domains_yml(
        context,
        [
            {"key": "wifi-analytics", "label": "WiFi 분석", "match_terms": ["wifi", "유동인구"]},
            {"key": "mdm-io", "label": "MDM/IO", "match_terms": ["mdm", "settopbox"]},
        ],
    )

    domains = module.load_domains(context)

    assert [d.key for d in domains] == ["wifi-analytics", "mdm-io"]
    assert domains[0].match_terms == ("wifi", "유동인구")


def test_resolve_domain_prefers_validated_proposal(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]
    _write_domains_yml(
        context, [{"key": "wifi-analytics", "match_terms": ["wifi"]}, {"key": "mdm-io", "match_terms": ["mdm"]}]
    )
    domains = module.load_domains(context)

    domain = module.resolve_domain({"title": "mdm thing"}, domains, proposed="wifi-analytics")

    assert domain == "wifi-analytics"


def test_resolve_domain_falls_back_to_match_terms_then_unclassified(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    context = module.resolve_docs_contexts()[0]
    _write_domains_yml(
        context, [{"key": "wifi-analytics", "match_terms": ["wifi"]}]
    )
    domains = module.load_domains(context)

    fallback = module.resolve_domain(
        {"title": "WiFi 대시보드 집계", "index_terms": []}, domains, proposed="not-a-real-domain"
    )
    unclassified = module.resolve_domain({"title": "unrelated topic"}, domains, proposed=None)

    assert fallback == "wifi-analytics"
    assert unclassified == module.UNCLASSIFIED_DOMAIN


def test_main_assigns_domain_to_generated_adr_and_index(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    context = module.resolve_docs_contexts()[0]
    _write_domains_yml(
        context, [{"key": "wifi-analytics", "match_terms": ["wifi"]}, {"key": "mdm-io", "match_terms": ["mdm"]}]
    )
    (tmp_path / "README.md").write_text("ADR2 instructions", encoding="utf-8")

    aar_path = context.aar_dir / "wifi-note.md"
    aar_path.parent.mkdir(parents=True, exist_ok=True)
    aar_path.write_text("WiFi dashboard aggregation decision", encoding="utf-8")

    def fake_call_openai_json_object(system_prompt, user_content, model=None, *, instructions=None):
        if "isCandidate" in system_prompt or "candidate" in system_prompt.lower():
            return {"isCandidate": True, "decisionScope": "architecture-boundary"}
        return {
            "title": "WiFi 집계 규칙",
            "scope": "architecture",
            "decision": "WiFi 집계는 새 규칙을 따른다.",
            "domain": "wifi-analytics",
            "index_terms": ["wifi"],
        }

    monkeypatch.setattr(module, "call_openai_json_object", fake_call_openai_json_object)

    module.main()

    written = json.loads(context.index_path.read_text(encoding="utf-8"))
    assert written["items"][0]["domain"] == "wifi-analytics"

    adr_files = list(context.adr_dir.glob("ADR-*.md"))
    assert len(adr_files) == 1
    meta, _ = module.parse_front_matter(adr_files[0])
    assert meta["domain"] == "wifi-analytics"
