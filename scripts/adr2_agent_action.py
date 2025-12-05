#!/usr/bin/env python3
"""
ADR 2.0 agent-focused promotion script.

This script is meant to be run inside CI (GitHub Actions) to:
- Scan docs/ for AAR files (excluding docs/adr/)
- Detect AARs that should be promoted to ADRs
- Generate agent-friendly ADR markdown with structured front matter
- Maintain docs/adr/index.json so agents can quickly locate relevant ADRs

Requirements:
- OPENAI_API_KEY must be available in the environment.
- Optionally set OPENAI_MODEL (defaults to gpt-5.1).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import yaml
from openai import OpenAI, OpenAIError


class SafeYAMLDumper(yaml.SafeDumper):
    """Custom YAML dumper that safely handles special characters (@, :, #, {}, [], etc.)."""
    pass


# Pre-compile constants for performance
_YAML_SPECIAL_CHARS = frozenset(['@', ':', '#', '{', '}', '[', ']', '!', '&', '*', '?', '%', '>', '|'])


def str_representer(dumper, data):
    """Force quoting on strings to avoid YAML parsing issues with special characters."""
    if not data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')
    if '\n' in data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    # Fast path: check first char and whitespace (most common cases)
    if data[0] in _YAML_SPECIAL_CHARS or data != data.strip():
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')
    # Check for @ and # anywhere in string (common problematic chars)
    if '@' in data or '#' in data:
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='"')


SafeYAMLDumper.add_representer(str, str_representer)


ACTION_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv("ADR2_REPO_ROOT") or Path.cwd()).resolve()
DOCS_DIR = ROOT / "docs"
ADR_DIR = DOCS_DIR / "adr"
INDEX_PATH = ADR_DIR / "index.json"

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
DEFAULT_LANGUAGE = os.getenv("ADR2_LANGUAGE", "en")

# Brief context primer for LLMs so they understand ADR 2.0 vs AAR.
CONTEXT_PRIMER = """
You are working with ADR 2.0:
- AAR (Agent Analysis Record): natural-language reasoning stored under docs/ (except docs/adr/). Describes why a change was made; no code/diffs.
- ADR (Architecture Decision Record): formal, machine-readable decision doc stored under docs/adr/. Contains scope, decision, rationale, alternatives, consequences, validation rules, agent signals (importance, enforcement level), timestamps. Used by agents/CI to enforce architecture.
- Goal: promote only architectural decisions from AARs into ADRs, produce agent-friendly, succinct, declarative outputs.
""".strip()


def log(msg: str) -> None:
    print(f"[adr2] {msg}")
    sys.stdout.flush()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "adr"


def format_id(number: int) -> str:
    return f"ADR-{number:04d}"


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def parse_front_matter(path: Path) -> Tuple[Dict, str]:
    text = read_file(path)
    match = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if not match:
        return {}, text
    front_matter = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)
    return front_matter, body


def load_prompts() -> Dict[str, str]:
    prompts = {}
    search_roots = [ROOT, ACTION_ROOT]
    names = {
        "candidate": "adr-candidate-detect-prompt.md",
        "generate": "adr-generate-prompt.md",
        "rules": "validate-rule-prompt.md",
    }
    for key, filename in names.items():
        for base in search_roots:
            path = base / filename
            if path.exists():
                prompts[key] = read_file(path)
                break
    return prompts


@dataclass
class ADRCandidate:
    path: Path
    scope: str
    detection: Dict
    adr_payload: Dict


def call_openai_json(system_prompt: str, user_content: str, model: str = DEFAULT_MODEL) -> Dict:
    client = OpenAI()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
    except OpenAIError as exc:
        raise RuntimeError(f"OpenAI API call failed: {exc}") from exc

    content = response.choices[0].message.content or "{}"
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse JSON from model response: {content}") from exc


def detect_candidates(prompts: Dict[str, str]) -> Tuple[List[Tuple[Path, Dict]], List[Path]]:
    aar_paths = []
    if DOCS_DIR.exists():
        for path in DOCS_DIR.rglob("*.md"):
            if ADR_DIR in path.parents:
                continue
            aar_paths.append(path)

    log(f"Discovered {len(aar_paths)} AAR markdown file(s) under docs/.")

    if not aar_paths:
        return [], []

    if "candidate" not in prompts:
        raise RuntimeError("Candidate detection prompt missing.")

    candidates = []
    non_candidates = []
    system_prompt = f"{CONTEXT_PRIMER}\n\n{prompts['candidate']}"
    for path in aar_paths:
        detection = call_openai_json(system_prompt, read_file(path))
        if detection.get("isCandidate"):
            log(f"Candidate detected: {path} (scope={detection.get('decisionScope')})")
            candidates.append((path, detection))
        else:
            log(f"Non-candidate: {path}")
            non_candidates.append(path)
    return candidates, non_candidates


def build_generator_prompt(prompts: Dict[str, str]) -> str:
    language_hint = f"Write the ADR in {DEFAULT_LANGUAGE}."
    base = f"{CONTEXT_PRIMER}\n\n{language_hint}\n\n{prompts.get('generate', '')}".strip()
    schema_hint = (
        "Return ONLY a JSON object with keys:"
        ' {"title","scope","decision","context","rationale",'
        '"alternatives","consequences","validation_rules","agent_playbook",'
        '"agent_signals","related_suggestions","index_terms"}. '
        'Use short, declarative language for agents. '
        'Scope must be one of ["architecture","infrastructure","data-model","api","component"]. '
        "Alternatives and consequences must be arrays. "
        "Validation rules must be an array of declarative constraints. "
        "agent_playbook must be an array of 3-6 imperative, step-like directives for agents (when to enforce, how to detect drift, how to remediate). "
        "agent_signals must include importance (high/medium/low) and enforcement (must/should/monitor). "
        "related_suggestions is an array of titles/phrases that may match other ADRs. "
        "index_terms is an array of 3-7 short keywords for retrieval. "
        "Do not include markdown or prose outside of the JSON object."
    )
    return f"{base}\n\n{schema_hint}".strip()


def generate_adr_payload(prompts: Dict[str, str], aar_text: str, scope_hint: str) -> Dict:
    system_prompt = build_generator_prompt(prompts)
    payload = call_openai_json(system_prompt, aar_text)
    payload.setdefault("scope", scope_hint or "architecture")
    payload.setdefault("alternatives", [])
    payload.setdefault("consequences", [])
    payload.setdefault("validation_rules", [])
    payload.setdefault("agent_playbook", [])
    payload.setdefault("agent_signals", {"importance": "medium", "enforcement": "should"})
    payload.setdefault("related_suggestions", [])
    payload.setdefault("index_terms", [])
    return payload


def resolve_related(suggestions: List[str], catalog: List[Dict]) -> List[str]:
    resolved: List[str] = []
    for suggestion in suggestions or []:
        target = suggestion.lower()
        for item in catalog:
            title = str(item.get("title", "")).lower()
            if target and target in title:
                resolved.append(item["id"])
                break
    # preserve order, remove duplicates
    seen = set()
    unique = []
    for rid in resolved:
        if rid not in seen:
            seen.add(rid)
            unique.append(rid)
    return unique


def next_adr_id(catalog: List[Dict]) -> str:
    numbers = []
    for meta in catalog:
        raw_id = meta.get("id", "")
        match = re.search(r"(\d+)$", raw_id)
        if match:
            numbers.append(int(match.group(1)))
    return format_id(max(numbers) + 1 if numbers else 1)


def render_adr(markup: Dict, body: Dict) -> str:
    alternatives = body.get("alternatives") or []
    consequences = body.get("consequences") or []
    validation_rules = markup.get("validation_rules") or []
    agent_playbook = markup.get("agent_playbook") or []
    agent_signals = markup.get("agent_signals") or {"importance": "medium", "enforcement": "should"}

    alternatives_block = "\n".join(f"- {item}" for item in alternatives) or "- None recorded."
    consequences_block = "\n".join(f"- {item}" for item in consequences) or "- Not documented."
    validation_block = "\n".join(f"- {item}" for item in validation_rules) or "- No validation rules captured."
    playbook_block = "\n".join(f"- {item}" for item in agent_playbook) or "- No agent playbook provided."
    signals_block = f"- Importance: {agent_signals.get('importance', 'medium')}\n- Enforcement: {agent_signals.get('enforcement', 'should')}"

    index_terms = markup.get("index_terms") or []
    index_block = "\n".join(f"- {term}" for term in index_terms) or "- none"

    front_matter = {
        "id": markup["id"],
        "title": markup["title"],
        "scope": markup["scope"],
        "created_at": markup["created_at"],
        "updated_at": markup["updated_at"],
        "decision": markup["decision"],
        "related": markup.get("related", []),
        "validation_rules": validation_rules,
        "agent_playbook": agent_playbook,
        "agent_signals": agent_signals,
        "index_terms": index_terms,
        "context": body.get("context", "").strip(),
        "rationale": body.get("rationale", "").strip(),
        "alternatives": alternatives,
        "consequences": consequences,
    }

    # Hybrid format: structured front matter + minimal human-readable context body.
    yaml_output = yaml.dump(
        front_matter,
        Dumper=SafeYAMLDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=float('inf')
    )

    # Validate round-trip to ensure YAML can be parsed back correctly.
    try:
        parsed = yaml.safe_load(yaml_output)
        if parsed != front_matter:
            log("WARNING: YAML round-trip validation failed. Data may be corrupted.")
    except yaml.YAMLError as e:
        log(f"WARNING: Generated YAML cannot be parsed: {e}")

    return (
        "---\n"
        f"{yaml_output}"
        "---\n\n"
        "## Context (for humans)\n"
        f"{body.get('context', '').strip() or 'N/A'}\n"
    )


def catalog_existing_adrs() -> List[Dict]:
    catalog: List[Dict] = []
    if not ADR_DIR.exists():
        return catalog

    for path in ADR_DIR.glob("*.md"):
        meta, _ = parse_front_matter(path)
        if not meta:
            continue
        catalog.append(
            {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "scope": meta.get("scope"),
                "related": meta.get("related", []),
                "validation_rules": meta.get("validation_rules", []),
                "agent_playbook": meta.get("agent_playbook", []),
                "agent_signals": meta.get("agent_signals", {}),
                "path": str(path.relative_to(ROOT)),
                "decision": meta.get("decision"),
                "index_terms": meta.get("index_terms", []),
                "updated_at": meta.get("updated_at"),
            }
        )
    return catalog


def write_index(catalog: List[Dict]) -> None:
    def summarize(decision: str | None) -> str:
        if not decision:
            return ""
        decision = decision.strip().replace("\n", " ")
        return decision[:160] + ("…" if len(decision) > 160 else "")

    thin_items = []
    for item in catalog:
        thin_items.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "scope": item.get("scope"),
                "path": item.get("path"),
                "related": item.get("related", []),
                "index_terms": item.get("index_terms", []),
                "decision_summary": summarize(item.get("decision")),
                "agent_signals": item.get("agent_signals", {}),
                "updated_at": item.get("updated_at"),
            }
        )

    payload = {
        "generated_at": now_iso(),
        "count": len(thin_items),
        "items": sorted(thin_items, key=lambda c: c.get("id", "")),
    }
    write_file(INDEX_PATH, json.dumps(payload, indent=2, ensure_ascii=False))


def already_promoted(source_path: Path, catalog: List[Dict]) -> bool:
    rel = str(source_path.relative_to(ROOT))
    return any(entry.get("source") == rel for entry in catalog)


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required.")

    prompts = load_prompts()
    log(f"Repo root: {ROOT}")
    log(f"Using model: {DEFAULT_MODEL}")
    log(f"Language: {DEFAULT_LANGUAGE}")
    catalog = catalog_existing_adrs()
    log(f"Loaded catalog with {len(catalog)} existing ADR(s).")

    detections, non_candidates = detect_candidates(prompts)
    non_candidate_deletions: set[Path] = set()
    candidate_deletions: set[Path] = set()
    if not detections and not non_candidates:
        log("No ADR candidates found.")
        return

    new_catalog_entries: List[Dict] = []
    for path, detection in detections:
        if already_promoted(path, catalog):
            log(f"Skipping already promoted AAR: {path}")
            # still mark for deletion to avoid reprocessing noise
            non_candidate_deletions.add(path)
            continue

        scope_hint = detection.get("decisionScope", "architecture")
        aar_text = read_file(path)
        payload = generate_adr_payload(prompts, aar_text, scope_hint)

        adr_id = next_adr_id(catalog + new_catalog_entries)
        slug = slugify(payload.get("title", adr_id))
        adr_filename = f"{adr_id}-{slug}.md"
        adr_path = ADR_DIR / adr_filename

        related_ids = resolve_related(payload.get("related_suggestions", []), catalog + new_catalog_entries)

        markup = {
            "id": adr_id,
            "title": payload.get("title", adr_id),
            "scope": payload.get("scope", scope_hint),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "decision": payload.get("decision", "").strip(),
            "related": related_ids,
            "validation_rules": payload.get("validation_rules", []),
            "agent_playbook": payload.get("agent_playbook", []),
            "agent_signals": payload.get("agent_signals", {"importance": "medium", "enforcement": "should"}),
            "index_terms": payload.get("index_terms", []),
        }

        content = render_adr(markup, payload)
        write_file(adr_path, content)

        catalog_entry = {
            "id": adr_id,
            "title": markup["title"],
            "scope": markup["scope"],
            "related": related_ids,
            "validation_rules": markup["validation_rules"],
            "path": str(adr_path.relative_to(ROOT)),
            "decision": markup["decision"],
            "agent_playbook": markup["agent_playbook"],
            "agent_signals": markup["agent_signals"],
            "index_terms": markup["index_terms"],
            "updated_at": markup["updated_at"],
        }
        new_catalog_entries.append(catalog_entry)
        log(f"Generated ADR {adr_id} -> {adr_path}")
        candidate_deletions.add(path)

    # delete non-candidates and processed candidates
    to_delete = set(non_candidates) | non_candidate_deletions | candidate_deletions
    for path in to_delete:
        try:
            path.unlink()
            log(f"Deleted AAR: {path}")
        except Exception as exc:  # pragma: no cover - filesystem issue
            log(f"Failed to delete AAR {path}: {exc}")

    full_catalog = catalog + new_catalog_entries
    write_index(full_catalog)
    log(f"Index updated with {len(full_catalog)} entries at {INDEX_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CI helper
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)
