#!/usr/bin/env python3
"""
ADR 2.0 reconciliation script.

This script is meant to be run inside CI (GitHub Actions) to:
- Reconcile AARs with existing ADRs
- Backfill ownership and consolidate duplicate ADRs
- Deterministically rebuild configured docs/adr/index.json files

Requirements:
- Set LLM_PROVIDER to 'openai' (default) or 'claude'.
- For OpenAI: OPENAI_API_KEY must be available. Optionally set OPENAI_MODEL (defaults to gpt-5.1).
- For Claude: ANTHROPIC_API_KEY must be available. Optionally set CLAUDE_MODEL (defaults to claude-sonnet-4-6).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
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
INDEX_SCHEMA_VERSION = 2
VALID_OPERATIONS = {"reconcile", "consolidate", "index"}
VALID_RECONCILIATION_ACTIONS = {"covered", "amend", "create", "reject", "defer"}
CONSOLIDATION_REPORT: List[str] = []

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
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_front_matter_document(meta: Dict[str, Any], body: str) -> str:
    yaml_output = yaml.dump(
        meta,
        Dumper=SafeYAMLDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=float("inf"),
    )
    return f"---\n{yaml_output}---\n\n{body.lstrip()}"


def merge_unique(existing: Iterable[Any], additions: Iterable[Any]) -> List[Any]:
    merged: List[Any] = []
    seen: set[str] = set()
    for item in [*existing, *additions]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item).strip().lower()
        if key and key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def normalize_reconciliation_decision(value: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(value or {})
    action = str(result.get("action") or "").strip().lower()
    result["action"] = action if action in VALID_RECONCILIATION_ACTIONS else "defer"
    return result


def amend_existing_adr(path: Path, patch: Dict[str, Any]) -> None:
    meta, body = parse_front_matter(path)
    if not meta:
        raise ValueError(f"Cannot amend ADR without front matter: {display_path(path)}")

    addition = str(patch.get("decision_addition") or "").strip()
    if addition and addition not in str(meta.get("decision") or ""):
        meta["decision"] = " ".join(filter(None, [str(meta.get("decision") or "").strip(), addition]))

    rules = normalize_string_list(meta.get("validation_rules"))
    for replacement in patch.get("replace_validation_rules") or []:
        if not isinstance(replacement, dict):
            continue
        existing = str(replacement.get("existing") or "").strip()
        new = str(replacement.get("replacement") or "").strip()
        if existing in rules and new:
            rules[rules.index(existing)] = new
    meta["validation_rules"] = merge_unique(rules, normalize_string_list(patch.get("add_validation_rules")))
    meta["agent_playbook"] = merge_unique(
        normalize_string_list(meta.get("agent_playbook")),
        normalize_string_list(patch.get("add_agent_playbook")),
    )
    meta["index_terms"] = merge_unique(
        normalize_string_list(meta.get("index_terms")),
        normalize_string_list(patch.get("add_index_terms")),
    )
    for field in ("owns", "contracts"):
        meta[field] = merge_unique(meta.get(field) or [], patch.get(f"add_{field}") or [])
    if patch.get("applies_to"):
        current = meta.get("applies_to") if isinstance(meta.get("applies_to"), dict) else {}
        incoming = patch["applies_to"] if isinstance(patch["applies_to"], dict) else {}
        meta["applies_to"] = {
            "paths": merge_unique(current.get("paths") or [], incoming.get("paths") or []),
            "symbols": merge_unique(current.get("symbols") or [], incoming.get("symbols") or []),
        }
    meta["updated_at"] = str(patch.get("updated_at") or now_iso())
    write_file(path, render_front_matter_document(meta, body))


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
        "ownership": "ownership-backfill-prompt.md",
        "consolidate": "adr-consolidate-prompt.md",
    }
    for key, filename in names.items():
        for base in search_roots:
            path = base / filename
            if path.exists():
                prompts[key] = read_file(path)
                break
    return prompts


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


def stringify(value: Any) -> str:
    """Flatten an arbitrary JSON-ish value into a plain string for matching."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(stringify(item) for item in value)
    if isinstance(value, dict):
        return " ".join(f"{key} {stringify(item)}" for key, item in value.items())
    return str(value)


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


def build_generator_prompt(
    prompts: Dict[str, str], domains: List[Domain] | None = None
) -> str:
    language_hint = f"Write the ADR in {DEFAULT_LANGUAGE}."
    base = (
        f"{language_hint}\n\n{prompts.get('generate', '')}".strip()
    )
    domain_key = '"domain",' if domains else ""
    schema_hint = (
        "Return ONLY a JSON object with keys:"
        ' {"title","scope","decision","context","rationale",'
        '"alternatives","consequences","validation_rules","agent_playbook",'
        f'"agent_signals","related_suggestions","index_terms","owns","contracts","applies_to",{domain_key}}}. '
        "Use short, declarative language for agents. "
        'Scope must be one of ["architecture","infrastructure","data-model","api","component"]. '
        "Alternatives and consequences must be arrays. "
        "Validation rules must be an array of declarative constraints. "
        "agent_playbook must be an array of 3-6 imperative, step-like directives for agents (when to enforce, how to detect drift, how to remediate). "
        "agent_signals must include importance (high/medium/low) and enforcement (must/should/monitor). "
        "related_suggestions is an array of titles/phrases that may match other ADRs. "
        "index_terms is an array of 3-7 short keywords for retrieval. "
        "owns is an array of {type,key} authoritative boundaries. contracts is an array of {id,role} entries. "
        "applies_to is an object with paths and symbols arrays; omit unsupported paths or symbols. "
        "Do not include markdown or prose outside of the JSON object."
    )
    if domains:
        domain_list = ", ".join(f'"{d.key}"' for d in domains)
        schema_hint += (
            f" domain must be exactly one key from this taxonomy based on the"
            f" ADR's primary authoritative boundary: [{domain_list}]. If none"
            " fit well, omit the domain field rather than guessing."
        )
    return f"{base}\n\n{schema_hint}".strip()


def generate_adr_payload(
    prompts: Dict[str, str],
    aar_text: str,
    scope_hint: str,
    domains: List[Domain] | None = None,
) -> Dict:
    system_prompt = build_generator_prompt(prompts, domains)
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
    payload.setdefault("domain", "")
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
    }
    # domain is only emitted when a docs dir has opted in via domains.yml, so
    # ADRs in repos/apps without a taxonomy stay byte-for-byte unaffected.
    if markup.get("domain"):
        front_matter["domain"] = markup["domain"]
    front_matter.update(
        {
            "created_at": markup["created_at"],
            "updated_at": markup["updated_at"],
            "decision": markup["decision"],
            "related": markup.get("related", []),
            "owns": markup.get("owns", []),
            "contracts": markup.get("contracts", []),
            "applies_to": markup.get("applies_to", {}),
            "relations": markup.get(
                "relations", {"related": markup.get("related", []), "depends_on": [], "supersedes": []}
            ),
            "validation_rules": validation_rules,
            "agent_playbook": agent_playbook,
            "agent_signals": agent_signals,
            "index_terms": index_terms,
            "context": body.get("context", "").strip(),
            "rationale": body.get("rationale", "").strip(),
            "alternatives": alternatives,
            "consequences": consequences,
        }
    )

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
                "domain": meta.get("domain"),
                "related": meta.get("related", []),
                "relations": meta.get("relations", {}),
                "owns": meta.get("owns", []),
                "contracts": meta.get("contracts", []),
                "applies_to": meta.get("applies_to", {}),
                "validation_rules": meta.get("validation_rules", []),
                "agent_playbook": meta.get("agent_playbook", []),
                "agent_signals": meta.get("agent_signals", {}),
                "path": display_path(path),
                "decision": meta.get("decision"),
                "created_at": meta.get("created_at"),
                "index_terms": meta.get("index_terms", []),
                "updated_at": meta.get("updated_at"),
            }
        )
    return catalog


def catalog_superseded_ids(context: DocsContext) -> set[str]:
    ids: set[str] = set()
    for path in (context.adr_dir / "superseded").glob("*.md"):
        meta, _ = parse_front_matter(path)
        if meta.get("id"):
            ids.add(str(meta["id"]))
    return ids


def validate_catalog(
    catalog: List[Dict],
    *,
    require_ownership: bool = False,
    superseded_ids: Iterable[str] = (),
) -> List[str]:
    errors: List[str] = []
    active_ids = {str(item.get("id") or "") for item in catalog}
    valid_relation_ids = active_ids | {str(value) for value in superseded_ids}
    owners: Dict[tuple[str, str], str] = {}
    producers: Dict[str, str] = {}

    for item in catalog:
        adr_id = str(item.get("id") or "<unknown>")
        if require_ownership and not ownership_keys(item):
            errors.append(f"{adr_id}: ownership is required")
        if require_ownership and str(item.get("domain") or "") in ("", UNCLASSIFIED_DOMAIN):
            errors.append(f"{adr_id}: valid domain is required")

        for owner in ownership_keys(item):
            previous = owners.setdefault(owner, adr_id)
            if previous != adr_id:
                errors.append(f"ownership {owner[0]}:{owner[1]} is duplicated by {previous} and {adr_id}")

        for contract in item.get("contracts") or []:
            if not isinstance(contract, dict) or str(contract.get("role") or "").lower() != "producer":
                continue
            contract_id = str(contract.get("id") or "").strip()
            if not contract_id:
                continue
            previous = producers.setdefault(contract_id, adr_id)
            if previous != adr_id:
                errors.append(f"contract producer {contract_id} is duplicated by {previous} and {adr_id}")

        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        for relation in ("related", "depends_on", "supersedes"):
            for target in normalize_string_list(relations.get(relation)):
                if target not in valid_relation_ids:
                    errors.append(f"{adr_id}: broken {relation} relation to {target}")
    return errors


def validate_contract_producers(catalogs: Iterable[List[Dict]]) -> List[str]:
    errors: List[str] = []
    producers: Dict[str, str] = {}
    for catalog in catalogs:
        for item in catalog:
            label = str(item.get("id") or item.get("path") or "<unknown>")
            for contract in item.get("contracts") or []:
                if not isinstance(contract, dict) or str(contract.get("role") or "").lower() != "producer":
                    continue
                contract_id = str(contract.get("id") or "").strip()
                if not contract_id:
                    continue
                previous = producers.setdefault(contract_id, label)
                if previous != label:
                    errors.append(f"contract producer {contract_id} is duplicated by {previous} and {label}")
    return errors


def build_index_payload(catalog: List[Dict]) -> Dict[str, Any]:
    def summarize(decision: str | None) -> str:
        if not decision:
            return ""
        decision = decision.strip().replace("\n", " ")
        return decision[:160] + ("…" if len(decision) > 160 else "")

    thin_items = []
    for item in catalog:
        thin_item = {
            "id": item.get("id"),
            "title": item.get("title"),
            "scope": item.get("scope"),
        }
        if item.get("domain"):
            thin_item["domain"] = item.get("domain")
        thin_item.update(
            {
                "path": item.get("path"),
                "related": item.get("related", []),
                "relations": item.get("relations", {}),
                "owns": item.get("owns", []),
                "contracts": item.get("contracts", []),
                "applies_to": item.get("applies_to", {}),
                "index_terms": item.get("index_terms", []),
                "decision_summary": summarize(item.get("decision")),
                "validation_rules": item.get("validation_rules", []),
                "agent_playbook": item.get("agent_playbook", []),
                "agent_signals": item.get("agent_signals", {}),
                "updated_at": item.get("updated_at"),
            }
        )
        thin_items.append(thin_item)

    items = sorted(thin_items, key=lambda c: c.get("id", ""))
    source = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    updated = sorted(str(item.get("updated_at") or "") for item in items)
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "generated_at": updated[-1] if updated else "",
        "count": len(items),
        "items": items,
    }


def write_index(catalog: List[Dict], context: DocsContext) -> None:
    payload = build_index_payload(catalog)

    # Regenerating unconditionally (see main()) means most runs find zero
    # drift. Rewriting the file anyway would still bump generated_at every
    # single time, turning every run into a spurious diff/commit even when
    # nothing about the ADR corpus actually changed. Only write when the
    # actual entries differ from what's already on disk.
    if context.index_path.exists():
        try:
            existing = json.loads(read_file(context.index_path))
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing == payload:
            log(f"Index unchanged at {context.index_path}; skipping rewrite.")
            return
    write_file(context.index_path, json.dumps(payload, indent=2, ensure_ascii=False))
    log(f"Index updated with {len(payload['items'])} entries at {context.index_path}")


def consolidate_adr_files(context: DocsContext, canonical_path: Path, duplicate_paths: List[Path]) -> None:
    canonical, body = parse_front_matter(canonical_path)
    if not canonical:
        raise ValueError(f"Canonical ADR has no front matter: {display_path(canonical_path)}")
    relations = canonical.get("relations") if isinstance(canonical.get("relations"), dict) else {}
    supersedes = normalize_string_list(relations.get("supersedes"))

    for duplicate_path in duplicate_paths:
        duplicate, _ = parse_front_matter(duplicate_path)
        if not duplicate:
            raise ValueError(f"Duplicate ADR has no front matter: {display_path(duplicate_path)}")
        for field in ("validation_rules", "agent_playbook", "index_terms", "owns", "contracts"):
            canonical[field] = merge_unique(canonical.get(field) or [], duplicate.get(field) or [])
        duplicate_decision = str(duplicate.get("decision") or "").strip()
        if duplicate_decision and duplicate_decision not in str(canonical.get("decision") or ""):
            canonical["decision"] = " ".join(
                filter(None, [str(canonical.get("decision") or "").strip(), duplicate_decision])
            )
        duplicate_relations = duplicate.get("relations") if isinstance(duplicate.get("relations"), dict) else {}
        for relation in ("related", "depends_on"):
            relations[relation] = merge_unique(
                relations.get(relation) or [], duplicate_relations.get(relation) or duplicate.get(relation) or []
            )
        supersedes = merge_unique(supersedes, [duplicate.get("id"), *normalize_string_list(duplicate_relations.get("supersedes"))])
        destination = context.adr_dir / "superseded" / duplicate_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(duplicate_path), str(destination))

    relations["related"] = relations.get("related") or []
    relations["depends_on"] = relations.get("depends_on") or []
    relations["supersedes"] = supersedes
    canonical["relations"] = relations
    canonical["updated_at"] = now_iso()
    write_file(canonical_path, render_front_matter_document(canonical, body))


def ownership_keys(value: Dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in value.get("owns") or []:
        if not isinstance(item, dict):
            continue
        owner_type = str(item.get("type") or "").strip().lower()
        key = str(item.get("key") or "").strip().lower()
        if owner_type and key:
            keys.add((owner_type, key))
    return keys


def _search_tokens(value: Any) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9가-힣_]+", stringify(value))
        if len(token) > 1
    }


def shortlist_existing_adrs(aar_text: str, catalog: List[Dict], limit: int = 8) -> List[Dict]:
    query = _search_tokens(aar_text)
    ranked: List[tuple[int, Dict]] = []
    for item in catalog:
        searchable = {
            "title": item.get("title"),
            "domain": item.get("domain"),
            "decision": item.get("decision"),
            "index_terms": item.get("index_terms"),
            "owns": item.get("owns"),
            "contracts": item.get("contracts"),
            "applies_to": item.get("applies_to"),
            "validation_rules": item.get("validation_rules"),
            "relations": item.get("relations"),
        }
        score = len(query & _search_tokens(searchable))
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
    selected = [item for _, item in ranked[:limit]]
    selected_ids = {item.get("id") for item in selected}
    by_id = {item.get("id"): item for item in catalog}
    for item in list(selected):
        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        related = [*normalize_string_list(item.get("related")), *normalize_string_list(relations.get("related")), *normalize_string_list(relations.get("depends_on"))]
        for adr_id in related:
            if adr_id in by_id and adr_id not in selected_ids and len(selected) < limit:
                selected.append(by_id[adr_id])
                selected_ids.add(adr_id)
    minimum = min(5, len(catalog), limit)
    if len(selected) < minimum:
        for item in sorted(catalog, key=lambda value: str(value.get("id") or "")):
            if item.get("id") not in selected_ids:
                selected.append(item)
                selected_ids.add(item.get("id"))
            if len(selected) >= minimum:
                break
    return selected


def reconciliation_decision(prompts: Dict[str, str], aar_text: str, catalog: List[Dict]) -> Dict[str, Any]:
    candidates = shortlist_existing_adrs(aar_text, catalog)
    user_content = json.dumps(
        {
            "aar": aar_text,
            "existing_adr_candidates": candidates,
        },
        ensure_ascii=False,
        indent=2,
    )
    return normalize_reconciliation_decision(
        call_openai_json_object(
            prompts["candidate"],
            user_content,
            instructions=prompts.get("adr2", ""),
        )
    )


def create_adr_from_aar(
    prompts: Dict[str, str],
    context: DocsContext,
    aar_text: str,
    catalog: List[Dict],
    domains: List[Domain],
    scope_hint: str,
) -> Dict[str, Any] | None:
    payload = generate_adr_payload(prompts, aar_text, scope_hint, domains)
    maybe_enrich_validation_rules(prompts, payload)
    payload["alternatives"] = normalize_string_list(payload.get("alternatives"))
    payload["consequences"] = normalize_string_list(payload.get("consequences"))
    payload["validation_rules"] = normalize_string_list(payload.get("validation_rules"))
    payload["agent_playbook"] = normalize_string_list(payload.get("agent_playbook"))
    payload["index_terms"] = canonicalize_index_terms(
        payload.get("index_terms"), build_index_term_canonical_map(catalog)
    )
    payload["owns"] = payload.get("owns") or []
    payload["contracts"] = payload.get("contracts") or []
    payload["applies_to"] = payload.get("applies_to") or {}
    proposed_keys = ownership_keys(payload)
    if not proposed_keys:
        log("Deferred create because ownership metadata is missing.")
        return None
    if any(proposed_keys & ownership_keys(item) for item in catalog):
        log("Deferred create because an existing ADR owns the same boundary.")
        return None

    adr_id = next_adr_id(catalog)
    related_ids = resolve_related(payload.get("related_suggestions", []), catalog)
    domain = resolve_domain(payload, domains, proposed=payload.get("domain"))
    now = now_iso()
    markup = {
        "id": adr_id,
        "title": payload.get("title", adr_id),
        "scope": normalize_adr_scope(payload.get("scope", scope_hint)),
        "domain": domain,
        "created_at": now,
        "updated_at": now,
        "decision": str(payload.get("decision") or "").strip(),
        "related": related_ids,
        "owns": payload["owns"],
        "contracts": payload["contracts"],
        "applies_to": payload["applies_to"],
        "relations": {"related": related_ids, "depends_on": [], "supersedes": []},
        "validation_rules": payload["validation_rules"],
        "agent_playbook": payload["agent_playbook"],
        "agent_signals": payload.get("agent_signals") or {"importance": "medium", "enforcement": "should"},
        "index_terms": payload["index_terms"],
    }
    path = context.adr_dir / f"{adr_id}-{slugify(markup['title'])}.md"
    write_file(path, render_adr(markup, payload))
    log(f"Generated ADR {adr_id} -> {path}")
    entry = dict(markup)
    entry["path"] = display_path(path)
    return entry


def reconcile_context(prompts: Dict[str, str], context: DocsContext, catalog: List[Dict], domains: List[Domain]) -> bool:
    if not context.aar_dir.exists():
        return False
    processed = False
    by_id = {str(item.get("id")): item for item in catalog}
    for aar_path in sorted(context.aar_dir.rglob("*.md")):
        aar_text = read_file(aar_path)
        decision = reconciliation_decision(prompts, aar_text, catalog)
        action = decision["action"]
        log(f"Reconciliation {action}: {display_path(aar_path)}")
        if action == "defer":
            continue
        if action == "covered":
            target_id = str(decision.get("target_adr_id") or "")
            if target_id not in by_id:
                log(f"Deferred covered because target ADR was not found: {target_id!r}")
                continue
        if action == "amend":
            target_id = str(decision.get("target_adr_id") or "")
            target = by_id.get(target_id)
            if not target:
                log(f"Deferred amend because target ADR was not found: {target_id!r}")
                continue
            amend_existing_adr(resolve_repo_path(str(target["path"])), decision)
        elif action == "create":
            created = create_adr_from_aar(
                prompts,
                context,
                aar_text,
                catalog,
                domains,
                str(decision.get("decision_scope") or DEFAULT_ADR_SCOPE),
            )
            if created is None:
                continue
            catalog.append(created)
            by_id[str(created["id"])] = created
        aar_path.unlink()
        processed = True
    return processed


def _metadata_from_path(path: Path) -> Dict[str, Any]:
    meta, _ = parse_front_matter(path)
    return meta


def backfill_ownership(prompts: Dict[str, str], catalog: List[Dict], domains: List[Domain]) -> bool:
    changed = False
    for item in catalog:
        needs_ownership = not ownership_keys(item)
        resolved_domain = resolve_domain(item, domains, item.get("domain")) if domains else ""
        needs_domain = bool(domains) and normalize_domain(item.get("domain"), domains) is None
        if not needs_ownership and not needs_domain:
            continue
        path = resolve_repo_path(str(item["path"]))
        meta, body = parse_front_matter(path)
        if needs_ownership:
            proposed = call_openai_json_object(
                prompts["ownership"],
                json.dumps(meta, ensure_ascii=False, indent=2),
                instructions=prompts.get("adr2", ""),
            )
            owns = [entry for entry in proposed.get("owns") or [] if isinstance(entry, dict)]
            if not owns:
                log(f"Ownership backfill deferred: {item.get('id')}")
                continue
            meta["owns"] = owns
            meta["contracts"] = [entry for entry in proposed.get("contracts") or [] if isinstance(entry, dict)]
            applies = proposed.get("applies_to") if isinstance(proposed.get("applies_to"), dict) else {}
            meta["applies_to"] = {
                "paths": normalize_string_list(applies.get("paths")),
                "symbols": normalize_string_list(applies.get("symbols")),
            }
        if domains:
            meta["domain"] = resolve_domain(meta, domains, meta.get("domain"))
        current_relations = meta.get("relations") if isinstance(meta.get("relations"), dict) else {}
        meta["relations"] = {
            "related": merge_unique(current_relations.get("related") or [], meta.get("related") or []),
            "depends_on": normalize_string_list(current_relations.get("depends_on")),
            "supersedes": normalize_string_list(current_relations.get("supersedes")),
        }
        write_file(path, render_front_matter_document(meta, body))
        item.update(
            {
                "owns": meta["owns"],
                "contracts": meta["contracts"],
                "applies_to": meta["applies_to"],
                "relations": meta["relations"],
                "domain": meta.get("domain"),
            }
        )
        changed = True
    return changed


def ownership_clusters(catalog: List[Dict]) -> List[List[Dict]]:
    parents = list(range(len(catalog)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    owners: Dict[tuple[str, str], int] = {}
    producers: Dict[str, int] = {}
    for index, item in enumerate(catalog):
        for key in ownership_keys(item):
            if key in owners:
                union(index, owners[key])
            else:
                owners[key] = index
        for contract in item.get("contracts") or []:
            if not isinstance(contract, dict) or str(contract.get("role") or "").lower() != "producer":
                continue
            contract_id = str(contract.get("id") or "").strip().lower()
            if contract_id in producers:
                union(index, producers[contract_id])
            elif contract_id:
                producers[contract_id] = index

    grouped: Dict[int, List[Dict]] = {}
    for index, item in enumerate(catalog):
        grouped.setdefault(find(index), []).append(item)
    return [items for items in grouped.values() if len(items) > 1]


def select_canonical(cluster: List[Dict], catalog: List[Dict]) -> Dict:
    referenced = {
        item.get("id"): sum(
            item.get("id") in normalize_string_list(other.get("related"))
            or item.get("id") in normalize_string_list((other.get("relations") or {}).get("related"))
            for other in catalog
        )
        for item in cluster
    }

    def rank(item: Dict) -> tuple:
        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        return (
            -bool(relations.get("supersedes")),
            -referenced.get(item.get("id"), 0),
            -len(item.get("validation_rules") or []),
            str(item.get("created_at") or "9999"),
            str(item.get("id") or ""),
        )

    return sorted(cluster, key=rank)[0]


def apply_ownership_updates(cluster: List[Dict], updates: Any) -> bool:
    by_id = {str(item.get("id")): item for item in cluster}
    changed = False
    for update in updates if isinstance(updates, list) else []:
        if not isinstance(update, dict):
            continue
        item = by_id.get(str(update.get("adr_id") or ""))
        owns = [value for value in update.get("owns") or [] if isinstance(value, dict)]
        if not item or not owns:
            continue
        path = resolve_repo_path(str(item["path"]))
        meta, body = parse_front_matter(path)
        meta["owns"] = owns
        meta["contracts"] = [value for value in update.get("contracts") or [] if isinstance(value, dict)]
        write_file(path, render_front_matter_document(meta, body))
        item["owns"] = meta["owns"]
        item["contracts"] = meta["contracts"]
        changed = True
    return changed


def consolidate_context(prompts: Dict[str, str], context: DocsContext, catalog: List[Dict], domains: List[Domain]) -> bool:
    changed = backfill_ownership(prompts, catalog, domains)
    for cluster in ownership_clusters(catalog):
        judgement = call_openai_json_object(
            prompts["consolidate"],
            json.dumps(cluster, ensure_ascii=False, indent=2),
            instructions=prompts.get("adr2", ""),
        )
        merge_allowed = all(
            (
                judgement.get("merge") is True,
                judgement.get("same_authoritative_boundary") is True,
                judgement.get("same_validation_responsibility") is True,
                judgement.get("independent_lifecycle") is False,
                judgement.get("independent_rollback") is False,
            )
        )
        if not merge_allowed:
            reclassified = apply_ownership_updates(cluster, judgement.get("ownership_updates"))
            log(f"Kept ownership cluster separate: {[item.get('id') for item in cluster]}")
            CONSOLIDATION_REPORT.append(
                f"- 독립 유지: {', '.join(str(item.get('id')) for item in cluster)}\n"
                f"  - 이유: {str(judgement.get('reason') or '책임 또는 lifecycle/rollback 경계가 독립적임')}\n"
                f"  - ownership 재분류: {'완료' if reclassified else '없음'}"
            )
            changed = reclassified or changed
            continue
        canonical = select_canonical(cluster, catalog)
        retired = [item for item in cluster if item.get("id") != canonical.get("id")]
        canonical_path = resolve_repo_path(str(canonical["path"]))
        duplicate_paths = [
            resolve_repo_path(str(item["path"])) for item in retired
        ]
        consolidate_adr_files(context, canonical_path, duplicate_paths)
        log(f"Consolidated {[item.get('id') for item in cluster]} into {canonical.get('id')}")
        preserved_rules = merge_unique([], [rule for item in retired for rule in item.get("validation_rules") or []])
        CONSOLIDATION_REPORT.append(
            f"- canonical: {canonical.get('id')}\n"
            f"  - retired: {', '.join(str(item.get('id')) for item in retired)}\n"
            f"  - 통합 이유: {str(judgement.get('reason') or 'authoritative boundary와 validation 책임이 일치함')}\n"
            f"  - 보존 규칙: {', '.join(str(rule) for rule in preserved_rules) or '없음'}\n"
            "  - 교체 규칙: 없음"
        )
        changed = True
    return changed


def main() -> None:
    PARSE_FAILURES.clear()
    CONSOLIDATION_REPORT.clear()
    operation = str(os.getenv("ADR2_OPERATION") or "reconcile").strip().lower()
    require_ownership = str(os.getenv("ADR2_REQUIRE_OWNERSHIP") or "false").lower() in {"1", "true", "yes"}
    if operation not in VALID_OPERATIONS:
        raise SystemExit(f"ADR2_OPERATION must be one of {sorted(VALID_OPERATIONS)}")

    if operation == "index":
        log(f"Repo root: {ROOT}")
        log("Operation: index")
        indexed: List[tuple[DocsContext, List[Dict]]] = []
        for context in resolve_docs_contexts():
            catalog = catalog_existing_adrs(context)
            errors = validate_catalog(
                catalog,
                require_ownership=require_ownership,
                superseded_ids=catalog_superseded_ids(context),
            )
            if errors:
                raise RuntimeError("Invalid ADR catalog:\n- " + "\n- ".join(errors))
            indexed.append((context, catalog))
        producer_errors = validate_contract_producers(catalog for _, catalog in indexed)
        if producer_errors:
            raise RuntimeError("Invalid ADR catalog:\n- " + "\n- ".join(producer_errors))
        for context, catalog in indexed:
            write_index(catalog, context)
        if PARSE_FAILURES:
            raise RuntimeError(
                f"{len(PARSE_FAILURES)} ADR file(s) failed front matter parsing; fix them before indexing."
            )
        return

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
    log(f"Operation: {operation}")
    log(f"Language: {DEFAULT_LANGUAGE}")
    log("Agentic reasoning: on")
    processed_any = False

    resulting_catalogs: List[List[Dict]] = []
    for context in resolve_docs_contexts():
        log(f"Docs dir: {display_path(context.docs_dir)}")
        catalog = catalog_existing_adrs(context)
        log(f"Loaded catalog with {len(catalog)} existing ADR(s).")
        domains = load_domains(context)
        if domains:
            log(f"Loaded {len(domains)} domain(s) from {display_path(context.domains_path)}.")
        if operation == "reconcile":
            processed_any = reconcile_context(prompts, context, catalog, domains) or processed_any
        else:
            processed_any = consolidate_context(prompts, context, catalog, domains) or processed_any
        resulting_catalog = catalog_existing_adrs(context)
        errors = validate_catalog(
            resulting_catalog,
            require_ownership=require_ownership,
            superseded_ids=catalog_superseded_ids(context),
        )
        if errors:
            raise RuntimeError("Invalid ADR catalog:\n- " + "\n- ".join(errors))
        resulting_catalogs.append(resulting_catalog)

    producer_errors = validate_contract_producers(resulting_catalogs)
    if producer_errors:
        raise RuntimeError("Invalid ADR catalog:\n- " + "\n- ".join(producer_errors))

    if not processed_any:
        log("No ADR candidates found in any configured docs dir.")

    if operation == "consolidate" and os.getenv("ADR2_PR_BODY_PATH"):
        report = "# ADR 통합 결과\n\n" + ("\n".join(CONSOLIDATION_REPORT) or "통합 또는 독립 유지 판정 대상이 없습니다.") + "\n"
        write_file(Path(os.environ["ADR2_PR_BODY_PATH"]), report)

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
