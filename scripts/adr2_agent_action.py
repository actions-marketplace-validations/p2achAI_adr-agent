#!/usr/bin/env python3
"""
ADR 2.0 agent-focused promotion script.

This script is meant to be run inside CI (GitHub Actions) to:
- Scan configured docs/aar/ directories for AAR files
- Detect AARs that should be promoted to ADRs
- Generate agent-friendly ADR markdown with structured front matter
- Maintain configured docs/adr/index.json files so agents can quickly locate relevant ADRs

Requirements:
- Set LLM_PROVIDER to 'openai' (default) or 'claude'.
- For OpenAI: OPENAI_API_KEY must be available. Optionally set OPENAI_MODEL (defaults to gpt-5.1).
- For Claude: ANTHROPIC_API_KEY must be available. Optionally set CLAUDE_MODEL (defaults to claude-sonnet-4-6).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import anthropic
import yaml
from openai import OpenAI, OpenAIError


class SafeYAMLDumper(yaml.SafeDumper):
    """Custom YAML dumper that safely handles special characters (@, :, #, {}, [], etc.)."""

    pass


# Pre-compile constants for performance
_YAML_SPECIAL_CHARS = frozenset(
    ["@", ":", "#", "{", "}", "[", "]", "!", "&", "*", "?", "%", ">", "|"]
)


def str_representer(dumper, data):
    """Force quoting on strings to avoid YAML parsing issues with special characters."""
    if not data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    # Fast path: check first char and whitespace (most common cases)
    if data[0] in _YAML_SPECIAL_CHARS or data != data.strip():
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    # Check for @ and # anywhere in string (common problematic chars)
    if "@" in data or "#" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


SafeYAMLDumper.add_representer(str, str_representer)


ACTION_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv("ADR2_REPO_ROOT") or Path.cwd()).resolve()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()

if LLM_PROVIDER == "claude":
    DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
else:
    DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")

DEFAULT_LANGUAGE = os.getenv("ADR2_LANGUAGE", "en")
VALID_DECISION_SCOPES = {
    "api-contract",
    "architecture-boundary",
    "data-governance",
    "runtime-operations",
    "security-trust",
    "integration-contract",
    "migration-compatibility",
    "developer-platform",
    "minor-change",
}

# ADR front matter `scope` values the generator is allowed to emit. The prompt
# alone never held this contract, so new ADRs are coerced in code.
VALID_ADR_SCOPES = (
    "architecture",
    "infrastructure",
    "data-model",
    "api",
    "component",
)
DEFAULT_ADR_SCOPE = "architecture"

# Domain taxonomy: routing axis for the ADR tree index. `scope` is far too
# coarse to route on, so domains are curated per docs dir and validated here.
DOMAINS_FILENAME = "domains.yml"
TREE_FILENAME = "tree.json"
UNCLASSIFIED_DOMAIN = "unclassified"

# Agents load the tree root first, so it must stay small enough to be cheap.
ROOT_PAYLOAD_LIMIT_BYTES = 5120
DOMAIN_SUMMARY_LIMIT = 200
LEAF_SUMMARY_LIMIT = 120


def log(msg: str) -> None:
    print(f"[adr2] {msg}")
    sys.stdout.flush()


MAX_MODEL_ATTEMPTS = 2
PLANNER_MODEL = DEFAULT_MODEL
GPT5_REASONING_EFFORT = "medium"


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


def _quote_yaml_scalar(value: str) -> str:
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sanitize_front_matter(raw: str) -> str:
    lines = raw.splitlines()
    sanitized: List[str] = []
    in_block = False
    block_indent = 0
    key_line_re = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*)$")

    for line in lines:
        if in_block:
            if line.strip() == "":
                sanitized.append(line)
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent > block_indent:
                sanitized.append(line)
                continue
            in_block = False

        match = key_line_re.match(line)
        if not match:
            sanitized.append(line)
            continue

        indent, key, value = match.groups()
        value = value.strip()
        if value == "":
            sanitized.append(line)
            continue
        if value.startswith(("|", ">")):
            in_block = True
            block_indent = len(indent)
            sanitized.append(line)
            continue
        if value.startswith(('"', "'", "[", "{", "&", "*", "!", "@")):
            sanitized.append(line)
            continue

        if ": " in value:
            quoted = _quote_yaml_scalar(value)
            sanitized.append(f"{indent}{key}: {quoted}")
            continue

        sanitized.append(line)

    return "\n".join(sanitized)


# Populated by parse_front_matter() whenever a docs/adr/*.md file has a
# ``---\n...\n---`` front matter block that cannot be parsed as YAML, even
# after sanitization. main() surfaces this list and fails loudly instead of
# silently dropping the ADR from the generated index, which is what used to
# happen (ADR-0001 disappeared from the backend index this way). Files with
# no front matter block at all (e.g. a plain docs/adr/README.md) are not
# treated as failures -- they are simply not ADR files.
PARSE_FAILURES: List[Tuple[Path, str]] = []


def parse_front_matter(path: Path) -> Tuple[Dict, str]:
    text = read_file(path)
    match = re.match(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, re.S)
    if not match:
        return {}, text
    try:
        front_matter = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        sanitized = _sanitize_front_matter(match.group(1))
        try:
            front_matter = yaml.safe_load(sanitized) or {}
            log(f"WARNING: Sanitized front matter in {display_path(path)} after YAML error: {exc}")
        except yaml.YAMLError as exc2:
            reason = str(exc2).splitlines()[0]
            PARSE_FAILURES.append((path, reason))
            log(f"ERROR: Failed to parse front matter in {display_path(path)}: {reason}")
            return {}, text
    body = match.group(2)
    return front_matter, body


def load_prompts() -> Dict[str, str]:
    prompts = {}
    search_roots = [ROOT, ACTION_ROOT]
    names = {
        "adr2": "README.md",
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


@dataclass(frozen=True)
class DocsContext:
    docs_dir: Path
    aar_dir: Path
    adr_dir: Path
    index_path: Path
    tree_path: Path
    domains_path: Path


@dataclass(frozen=True)
class Domain:
    key: str
    label: str
    match_terms: Tuple[str, ...]
    parent: str | None


def _split_path_list(value: str) -> List[str]:
    return [item.strip() for item in re.split(r"[\n,]+", value) if item.strip()]


def resolve_repo_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def resolve_docs_contexts() -> List[DocsContext]:
    docs_dir_values = _split_path_list(os.getenv("ADR2_DOCS_DIRS", "")) or ["docs"]
    contexts = []
    seen: set[Path] = set()
    for value in docs_dir_values:
        docs_dir = resolve_repo_path(value)
        if docs_dir in seen:
            continue
        seen.add(docs_dir)
        contexts.append(
            DocsContext(
                docs_dir=docs_dir,
                aar_dir=docs_dir / "aar",
                adr_dir=docs_dir / "adr",
                index_path=docs_dir / "adr" / "index.json",
                tree_path=docs_dir / "adr" / TREE_FILENAME,
                domains_path=docs_dir / "adr" / DOMAINS_FILENAME,
            )
        )
    return contexts


def load_domains(context: DocsContext) -> List[Domain]:
    """Load the curated domain taxonomy for a docs dir.

    A missing file disables domain classification and tree generation for that
    docs dir, keeping the previous behaviour intact.
    """
    if not context.domains_path.exists():
        return []

    try:
        raw = yaml.safe_load(read_file(context.domains_path)) or {}
    except yaml.YAMLError as exc:
        log(f"WARNING: Failed to parse {display_path(context.domains_path)}: {exc}")
        return []

    entries = raw.get("domains") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        log(f"WARNING: {display_path(context.domains_path)} has no 'domains' list.")
        return []

    domains: List[Domain] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        domains.append(
            Domain(
                key=key,
                label=str(entry.get("label") or key).strip(),
                match_terms=tuple(normalize_string_list(entry.get("match_terms"))),
                parent=(str(entry.get("parent")).strip() or None)
                if entry.get("parent")
                else None,
            )
        )

    known = {domain.key for domain in domains}
    for domain in domains:
        if domain.parent and domain.parent not in known:
            log(
                f"WARNING: domain '{domain.key}' references unknown parent "
                f"'{domain.parent}' in {display_path(context.domains_path)}."
            )
    return domains


def normalize_domain(value: Any, domains: Iterable[Domain]) -> str | None:
    """Accept a domain value only when it exists in the taxonomy."""
    if not value:
        return None
    candidate = str(value).strip().lower()
    if not candidate:
        return None
    for domain in domains:
        if domain.key.lower() == candidate:
            return domain.key
    return None


def _domain_haystack(record: Dict[str, Any]) -> str:
    parts = [
        stringify(record.get("title")),
        stringify(record.get("index_terms")),
        stringify(record.get("path")),
        stringify(record.get("decision")),
    ]
    return " ".join(part for part in parts if part).lower()


def match_domain_by_terms(
    record: Dict[str, Any], domains: Iterable[Domain]
) -> str | None:
    """Deterministic fallback classification based on curated match terms."""
    haystack = _domain_haystack(record)
    if not haystack:
        return None

    best_key: str | None = None
    best_score = 0
    for domain in domains:
        score = 0
        for term in domain.match_terms:
            needle = term.strip().lower()
            if needle and needle in haystack:
                score += 1
        # Ties resolve to the first taxonomy entry, keeping output deterministic.
        if score > best_score:
            best_score = score
            best_key = domain.key
    return best_key


def resolve_domain(
    record: Dict[str, Any],
    domains: Iterable[Domain],
    proposed: Any = None,
) -> str:
    """Resolve a domain: validated proposal, then terms, then unclassified."""
    domains = list(domains)
    if not domains:
        return ""

    validated = normalize_domain(proposed, domains)
    if validated:
        return validated
    if proposed:
        log(
            f"WARNING: proposed domain {str(proposed)!r} is not in the taxonomy; "
            "falling back to deterministic matching."
        )

    matched = match_domain_by_terms(record, domains)
    return matched or UNCLASSIFIED_DOMAIN


def normalize_adr_scope(value: Any) -> str:
    scope = str(value or "").strip()
    if scope in VALID_ADR_SCOPES:
        return scope
    if scope:
        log(
            f"WARNING: scope {scope!r} is outside the allowed set "
            f"{list(VALID_ADR_SCOPES)}; using {DEFAULT_ADR_SCOPE!r}."
        )
    return DEFAULT_ADR_SCOPE


_OPENAI_CLIENT: OpenAI | None = None
_ANTHROPIC_CLIENT: anthropic.Anthropic | None = None


def get_openai_client() -> OpenAI:
    global _OPENAI_CLIENT
    if _OPENAI_CLIENT is None:
        _OPENAI_CLIENT = OpenAI()
    return _OPENAI_CLIENT


def get_anthropic_client() -> anthropic.Anthropic:
    global _ANTHROPIC_CLIENT
    if _ANTHROPIC_CLIENT is None:
        _ANTHROPIC_CLIENT = anthropic.Anthropic()
    return _ANTHROPIC_CLIENT


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_json_from_text(text: str) -> Any:
    text = _strip_code_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Best-effort extraction when the model wraps JSON in extra prose.
    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.S)
        if match:
            return json.loads(match.group(0))
    raise json.JSONDecodeError("No JSON found", text, 0)


def call_openai_text(
    *,
    model: str,
    messages: List[Dict[str, str]],
    response_format: Dict | None = None,
    instructions: str | None = None,
) -> str:
    client = get_openai_client()
    try:
        # Prefer Responses API (needed for reasoning.effort on GPT-5.x).
        if hasattr(client, "responses"):
            kwargs: Dict[str, Any] = {"model": model, "input": messages}
            if instructions:
                kwargs["instructions"] = instructions
            if response_format is not None:
                # JSON mode (ensures valid JSON object output when the prompt requests it).
                if response_format.get("type") == "json_object":
                    kwargs["text"] = {"format": {"type": "json_object"}}

            if model.startswith("gpt-5.1"):
                kwargs["reasoning"] = {"effort": GPT5_REASONING_EFFORT}

            resp = client.responses.create(**kwargs)
            return getattr(resp, "output_text", "") or ""

        # Fallback for older SDKs that don't have Responses API.
        kwargs = {}
        if instructions:
            # Fold instructions into the first system message for ChatCompletions.
            if messages and messages[0].get("role") == "system":
                messages = [
                    {
                        "role": "system",
                        "content": f"{instructions}\n\n{messages[0].get('content','')}".strip(),
                    },
                    *messages[1:],
                ]
            else:
                messages = [{"role": "system", "content": instructions}, *messages]
        if response_format is not None:
            kwargs["response_format"] = response_format
        response = client.chat.completions.create(model=model, messages=messages, **kwargs)
    except OpenAIError as exc:
        raise RuntimeError(f"OpenAI API call failed: {exc}") from exc
    return response.choices[0].message.content or ""


def call_claude_text(
    *,
    model: str,
    messages: List[Dict[str, str]],
    response_format: Dict | None = None,
    instructions: str | None = None,
) -> str:
    """Call Anthropic Claude API with adaptive thinking (streaming)."""
    client = get_anthropic_client()

    # Build system prompt from instructions and system messages
    system_parts = []
    if instructions:
        system_parts.append(instructions)

    filtered_messages: List[Dict[str, str]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_parts.append(msg.get("content", ""))
        else:
            filtered_messages.append({"role": msg["role"], "content": msg["content"]})

    system = "\n\n".join(part for part in system_parts if part)

    # For JSON mode, reinforce via system prompt (Claude has no native json_object mode)
    if response_format and response_format.get("type") == "json_object":
        json_hint = "Return ONLY a valid JSON object. No prose, no markdown code fences."
        system = f"{system}\n\n{json_hint}" if system else json_hint

    create_kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "messages": filtered_messages,
    }
    if system:
        create_kwargs["system"] = system

    try:
        with client.messages.stream(**create_kwargs) as stream:
            final_message = stream.get_final_message()
    except anthropic.APIError as exc:
        raise RuntimeError(f"Anthropic API call failed: {exc}") from exc

    text_blocks = [block.text for block in final_message.content if block.type == "text"]
    return "".join(text_blocks)


def call_llm_text(
    *,
    model: str,
    messages: List[Dict[str, str]],
    response_format: Dict | None = None,
    instructions: str | None = None,
) -> str:
    """Dispatch to the configured LLM provider (openai or claude)."""
    if LLM_PROVIDER == "claude":
        return call_claude_text(
            model=model,
            messages=messages,
            response_format=response_format,
            instructions=instructions,
        )
    return call_openai_text(
        model=model,
        messages=messages,
        response_format=response_format,
        instructions=instructions,
    )


def call_openai_json_object(
    system_prompt: str,
    user_content: str,
    model: str = DEFAULT_MODEL,
    *,
    instructions: str | None = None,
) -> Dict[str, Any]:
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    last_content = ""
    for attempt in range(MAX_MODEL_ATTEMPTS):
        messages = list(base_messages)
        if attempt > 0:
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous reply was not valid JSON. Return ONLY a valid JSON object.",
                }
            )
        last_content = call_llm_text(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            instructions=instructions,
        )
        try:
            parsed = parse_json_from_text(last_content)
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    f"Expected JSON object but got {type(parsed).__name__}"
                )
            return parsed
        except Exception:
            if attempt == MAX_MODEL_ATTEMPTS - 1:
                raise RuntimeError(
                    f"Failed to parse JSON object from model response: {last_content}"
                )
    return {}


def normalize_candidate_decision(detection: Dict[str, Any]) -> Tuple[bool, str]:
    """Interpret detector output conservatively."""
    raw_candidate = detection.get("isCandidate")
    decision_scope = (detection.get("decisionScope") or "").strip()

    is_candidate = raw_candidate is True
    if not is_candidate:
        return False, decision_scope

    if decision_scope not in VALID_DECISION_SCOPES:
        return False, decision_scope

    if decision_scope == "minor-change":
        return False, decision_scope

    return True, decision_scope


def call_openai_json_value(
    system_prompt: str,
    user_content: str,
    model: str = DEFAULT_MODEL,
    *,
    instructions: str | None = None,
) -> Any:
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    last_content = ""
    for attempt in range(MAX_MODEL_ATTEMPTS):
        messages = list(base_messages)
        if attempt > 0:
            messages.append(
                {
                    "role": "user",
                    "content": "Return ONLY valid JSON (no prose, no markdown).",
                }
            )
        last_content = call_llm_text(
            model=model, messages=messages, instructions=instructions
        )
        try:
            return parse_json_from_text(last_content)
        except Exception:
            if attempt == MAX_MODEL_ATTEMPTS - 1:
                raise RuntimeError(
                    f"Failed to parse JSON from model response: {last_content}"
                )
    return None


def maybe_add_agentic_working_notes(aar_text: str) -> str:
    """멀티패스(선-분석 후-생성)로 모델 추론을 유도."""

    planner_system = (
        "You are a careful analyst. Read the AAR and produce compact working notes as JSON.\n"
        "Return ONLY a JSON object with keys: "
        '["summary","explicit_decisions","constraints","alternatives","consequences","validation_rule_candidates"].\n'
        "Each value must be a string or array of short strings. Be conservative; omit uncertain items."
    )
    notes = call_openai_json_object(planner_system, aar_text, model=PLANNER_MODEL)
    notes_json = json.dumps(notes, ensure_ascii=False, indent=2)
    return (
        f"{aar_text}\n\n"
        "----\n"
        "WORKING_NOTES_JSON (for your internal reasoning; do not repeat verbatim):\n"
        f"{notes_json}\n"
    )


def detect_candidates(
    prompts: Dict[str, str],
    context: DocsContext,
) -> Tuple[List[Tuple[Path, Dict]], List[Path]]:
    aar_paths = []
    if context.aar_dir.exists():
        for path in context.aar_dir.rglob("*.md"):
            aar_paths.append(path)
    else:
        log(f"{display_path(context.aar_dir)} not found; skipping AAR scan.")
        return [], []

    log(
        f"Discovered {len(aar_paths)} AAR markdown file(s) under {display_path(context.aar_dir)}/."
    )

    if not aar_paths:
        return [], []

    if "candidate" not in prompts:
        raise RuntimeError("Candidate detection prompt missing.")

    candidates = []
    non_candidates = []
    instructions = prompts.get("adr2", "")
    system_prompt = prompts["candidate"]
    for path in aar_paths:
        aar_text = read_file(path)
        detection = call_openai_json_object(
            system_prompt,
            maybe_add_agentic_working_notes(aar_text),
            instructions=instructions,
        )
        is_candidate, decision_scope = normalize_candidate_decision(detection)
        if is_candidate:
            log(
                f"Candidate detected: {path} (scope={decision_scope or detection.get('decisionScope')})"
            )
            candidates.append((path, detection))
        else:
            reason = ""
            if detection.get("isCandidate") is not True:
                reason = f" (isCandidate={detection.get('isCandidate')!r})"
            elif decision_scope == "minor-change":
                reason = " (scope=minor-change)"
            elif decision_scope not in VALID_DECISION_SCOPES:
                reason = f" (invalid scope={decision_scope!r})"
            log(f"Non-candidate: {path}{reason}")
            non_candidates.append(path)
    return candidates, non_candidates


def build_generator_prompt(prompts: Dict[str, str]) -> str:
    language_hint = f"Write the ADR in {DEFAULT_LANGUAGE}."
    base = (
        f"{language_hint}\n\n{prompts.get('generate', '')}".strip()
    )
    schema_hint = (
        "Return ONLY a JSON object with keys:"
        ' {"title","scope","decision","context","rationale",'
        '"alternatives","consequences","validation_rules","agent_playbook",'
        '"agent_signals","related_suggestions","index_terms"}. '
        "Use short, declarative language for agents. "
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


def generate_adr_payload(
    prompts: Dict[str, str], aar_text: str, scope_hint: str
) -> Dict:
    system_prompt = build_generator_prompt(prompts)
    instructions = prompts.get("adr2", "")
    payload = call_openai_json_object(
        system_prompt,
        maybe_add_agentic_working_notes(aar_text),
        instructions=instructions,
    )
    payload.setdefault("scope", scope_hint or "architecture")
    payload.setdefault("alternatives", [])
    payload.setdefault("consequences", [])
    payload.setdefault("validation_rules", [])
    payload.setdefault("agent_playbook", [])
    payload.setdefault(
        "agent_signals", {"importance": "medium", "enforcement": "should"}
    )
    payload.setdefault("related_suggestions", [])
    payload.setdefault("index_terms", [])
    return payload


def normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        v = value.strip()
        return [v] if v else []
    return [str(value).strip()]


# index_terms drift: 92%+ of index_terms across the corpus are hapax (used by
# a single ADR), and existing ones frequently disagree on casing/separators
# for the same concept (e.g. "measurement-v2" vs "Measurement V2", "settopbox"
# vs "set-top-box"). Rather than impose one universal casing rule on every new
# term (which would fight established acronym conventions like MDM/WiFi/RBAC),
# new terms are aliased against whatever canonical form the existing catalog
# already established for the same underlying concept.
_INDEX_TERM_SEPARATORS_RE = re.compile(r"[\s_/-]+")


def index_term_key(term: str) -> str:
    """Normalize a term to a separator/case-insensitive identity key."""
    return _INDEX_TERM_SEPARATORS_RE.sub("", term.strip().lower())


def build_index_term_canonical_map(catalog: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Pick one canonical surface form per index-term identity key.

    Ties (including single-occurrence terms) resolve alphabetically so the
    map is deterministic across runs.
    """
    forms_by_key: Dict[str, Dict[str, int]] = {}
    for entry in catalog:
        for term in normalize_string_list(entry.get("index_terms")):
            key = index_term_key(term)
            if not key:
                continue
            counts = forms_by_key.setdefault(key, {})
            counts[term] = counts.get(term, 0) + 1

    canonical: Dict[str, str] = {}
    for key, counts in forms_by_key.items():
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        canonical[key] = ranked[0][0]
    return canonical


def canonicalize_index_terms(
    terms: Iterable[str], canonical_map: Dict[str, str]
) -> List[str]:
    """Alias each term to its established canonical spelling when known.

    Terms whose identity key is not yet in the map (genuinely new concepts)
    pass through unchanged aside from whitespace trimming.
    """
    resolved: List[str] = []
    seen: set[str] = set()
    for term in normalize_string_list(terms):
        key = index_term_key(term)
        canonical_term = canonical_map.get(key, term) if key else term
        if canonical_term not in seen:
            seen.add(canonical_term)
            resolved.append(canonical_term)
    return resolved


def maybe_enrich_validation_rules(prompts: Dict[str, str], payload: Dict[str, Any]) -> None:
    """
    - 생성된 ADR의 핵심 텍스트로부터 추가 validation_rules를 추출해 병합.
    """
    rules_prompt = prompts.get("rules")
    if not rules_prompt:
        return
    instructions = prompts.get("adr2", "")

    seed_text = "\n\n".join(
        [
            f"Title: {payload.get('title','')}",
            f"Decision: {payload.get('decision','')}",
            f"Context: {payload.get('context','')}",
            f"Rationale: {payload.get('rationale','')}",
        ]
    ).strip()
    if not seed_text:
        return

    extracted_obj = call_openai_json_object(
        rules_prompt, seed_text, instructions=instructions
    )
    extracted = extracted_obj.get("rules")
    if not isinstance(extracted, list):
        return

    existing = normalize_string_list(payload.get("validation_rules"))
    additional = normalize_string_list(extracted)
    merged: List[str] = []
    seen: set[str] = set()
    for rule in existing + additional:
        key = rule.lower()
        if key not in seen:
            seen.add(key)
            merged.append(rule)
    payload["validation_rules"] = merged


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
    agent_signals = markup.get("agent_signals") or {
        "importance": "medium",
        "enforcement": "should",
    }

    alternatives_block = (
        "\n".join(f"- {item}" for item in alternatives) or "- None recorded."
    )
    consequences_block = (
        "\n".join(f"- {item}" for item in consequences) or "- Not documented."
    )
    validation_block = (
        "\n".join(f"- {item}" for item in validation_rules)
        or "- No validation rules captured."
    )
    playbook_block = (
        "\n".join(f"- {item}" for item in agent_playbook)
        or "- No agent playbook provided."
    )
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
        width=float("inf"),
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


def catalog_existing_adrs(context: DocsContext) -> List[Dict]:
    catalog: List[Dict] = []
    if not context.adr_dir.exists():
        return catalog

    for path in context.adr_dir.glob("*.md"):
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
                "path": display_path(path),
                "decision": meta.get("decision"),
                "index_terms": meta.get("index_terms", []),
                "updated_at": meta.get("updated_at"),
            }
        )
    return catalog


def write_index(catalog: List[Dict], context: DocsContext) -> None:
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
    write_file(context.index_path, json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> None:
    PARSE_FAILURES.clear()

    if LLM_PROVIDER == "claude":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is required when LLM_PROVIDER=claude.")
        log(f"Provider: Claude (model={DEFAULT_MODEL})")
    else:
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is required when LLM_PROVIDER=openai.")
        log(f"Provider: OpenAI (model={DEFAULT_MODEL})")

    prompts = load_prompts()
    if "adr2" not in prompts:
        raise SystemExit("README.md prompt (adr2) is required.")
    log(f"Repo root: {ROOT}")
    log(f"Language: {DEFAULT_LANGUAGE}")
    log("Agentic reasoning: on")
    processed_any = False

    for context in resolve_docs_contexts():
        log(f"Docs dir: {display_path(context.docs_dir)}")
        catalog = catalog_existing_adrs(context)
        log(f"Loaded catalog with {len(catalog)} existing ADR(s).")
        index_term_canonical_map = build_index_term_canonical_map(catalog)

        detections, non_candidates = detect_candidates(prompts, context)
        non_candidate_deletions: set[Path] = set()
        candidate_deletions: set[Path] = set()
        new_catalog_entries: List[Dict] = []

        if not detections and not non_candidates:
            log("No ADR candidates found; index will still be regenerated from disk.")
        else:
            processed_any = True
            for path, detection in detections:
                scope_hint = detection.get("decisionScope", "architecture")
                aar_text = read_file(path)
                payload = generate_adr_payload(prompts, aar_text, scope_hint)
                maybe_enrich_validation_rules(prompts, payload)
                payload["alternatives"] = normalize_string_list(payload.get("alternatives"))
                payload["consequences"] = normalize_string_list(payload.get("consequences"))
                payload["validation_rules"] = normalize_string_list(payload.get("validation_rules"))
                payload["agent_playbook"] = normalize_string_list(payload.get("agent_playbook"))
                payload["index_terms"] = canonicalize_index_terms(
                    payload.get("index_terms"), index_term_canonical_map
                )
                if not isinstance(payload.get("agent_signals"), dict):
                    payload["agent_signals"] = {"importance": "medium", "enforcement": "should"}

                adr_id = next_adr_id(catalog + new_catalog_entries)
                slug = slugify(payload.get("title", adr_id))
                adr_filename = f"{adr_id}-{slug}.md"
                adr_path = context.adr_dir / adr_filename

                related_ids = resolve_related(
                    payload.get("related_suggestions", []), catalog + new_catalog_entries
                )

                markup = {
                    "id": adr_id,
                    "title": payload.get("title", adr_id),
                    "scope": normalize_adr_scope(payload.get("scope", scope_hint)),
                    "created_at": now_iso(),
                    "updated_at": now_iso(),
                    "decision": payload.get("decision", "").strip(),
                    "related": related_ids,
                    "validation_rules": payload.get("validation_rules", []),
                    "agent_playbook": payload.get("agent_playbook", []),
                    "agent_signals": payload.get(
                        "agent_signals", {"importance": "medium", "enforcement": "should"}
                    ),
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
                    "path": display_path(adr_path),
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

        # Always regenerate the index from what is on disk, even when there are
        # no AAR promotion candidates this run. Otherwise manual edits to
        # existing ADR files (or their removal) never get absorbed until the
        # next promotion happens to fire, which can be an arbitrarily long time.
        full_catalog = catalog + new_catalog_entries
        write_index(full_catalog, context)
        log(f"Index updated with {len(full_catalog)} entries at {context.index_path}")

    if not processed_any:
        log("No ADR candidates found in any configured docs dir.")

    if PARSE_FAILURES:
        log(f"ERROR: {len(PARSE_FAILURES)} ADR file(s) failed front matter parsing:")
        for path, reason in PARSE_FAILURES:
            log(f"  - {display_path(path)}: {reason}")
        raise RuntimeError(
            f"{len(PARSE_FAILURES)} ADR file(s) failed front matter parsing; "
            "fix front matter before the index can be trusted (see log above)."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CI helper
        sys.stderr.write(f"ERROR: {exc}\n")
        sys.exit(1)
