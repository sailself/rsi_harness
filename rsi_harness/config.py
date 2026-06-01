from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when a harness config file is present but cannot be parsed or is invalid."""


@dataclass(frozen=True)
class VerifyCommandConfig:
    name: str
    run: str
    timeout_sec: int | None = None
    max_runtime_sec: int | None = None


@dataclass(frozen=True)
class VerifyConfig:
    timeout_sec: int = 180
    memory_mb: int | None = None
    commands: list[VerifyCommandConfig] = field(default_factory=list)
    changed_file_rules: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchConfig:
    experts: int = 1
    rounds: int = 1
    worktree: bool = False
    selector: str = "score"
    feedback_budget_chars: int = 12000
    experts_file: str = "experts.yaml"


@dataclass(frozen=True)
class HarnessConfig:
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    search: SearchConfig = field(default_factory=SearchConfig)


def load_config(path: str | Path | None = None) -> HarnessConfig:
    config_path = Path(path) if path else Path.cwd() / ".rsi.yaml"
    if not config_path.exists():
        return HarnessConfig()

    raw = config_path.read_text(encoding="utf-8")
    data = _load_structured_data(raw, config_path.suffix)
    return config_from_dict(data or {})


def config_from_dict(data: dict[str, Any]) -> HarnessConfig:
    verify_data = data.get("verify") or {}
    search_data = data.get("search") or {}
    commands = []
    for index, item in enumerate(verify_data.get("commands") or []):
        if isinstance(item, str):
            commands.append(VerifyCommandConfig(name=item, run=item))
        elif isinstance(item, dict):
            if "name" not in item or "run" not in item:
                raise ConfigError(
                    f"verify.commands[{index}] must define both 'name' and 'run' (got keys: {sorted(item)})"
                )
            commands.append(
                VerifyCommandConfig(
                    name=str(item["name"]),
                    run=str(item["run"]),
                    timeout_sec=_optional_int(item.get("timeout_sec"), key=f"verify.commands[{index}].timeout_sec"),
                    max_runtime_sec=_optional_int(
                        item.get("max_runtime_sec"), key=f"verify.commands[{index}].max_runtime_sec"
                    ),
                )
            )
        else:
            raise ConfigError(f"verify.commands[{index}] must be a string or mapping, got {type(item).__name__}")

    changed_rules = {}
    for pattern, rules in (verify_data.get("changed_file_rules") or {}).items():
        if isinstance(rules, str):
            changed_rules[str(pattern)] = [rules]
        else:
            changed_rules[str(pattern)] = [str(rule) for rule in rules]

    verify = VerifyConfig(
        timeout_sec=_required_int(verify_data.get("timeout_sec", 180), key="verify.timeout_sec"),
        memory_mb=_optional_int(verify_data.get("memory_mb"), key="verify.memory_mb"),
        commands=commands,
        changed_file_rules=changed_rules,
    )
    search = SearchConfig(
        experts=_required_int(search_data.get("experts", 1), key="search.experts"),
        rounds=_required_int(search_data.get("rounds", 1), key="search.rounds"),
        worktree=bool(search_data.get("worktree", False)),
        selector=str(search_data.get("selector", "score")),
        feedback_budget_chars=_required_int(
            search_data.get("feedback_budget_chars", 12000), key="search.feedback_budget_chars"
        ),
        experts_file=str(search_data.get("experts_file", "experts.yaml")),
    )
    return HarnessConfig(verify=verify, search=search)


def _optional_int(value: Any, key: str = "value") -> int | None:
    if value is None or value == "":
        return None
    return _required_int(value, key=key)


def _required_int(value: Any, key: str = "value") -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer, got {value!r}") from exc


def _load_structured_data(raw: str, suffix: str) -> dict[str, Any]:
    if suffix.lower() == ".json":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON config: {exc}") from exc

    try:
        import yaml  # type: ignore
    except ModuleNotFoundError:
        return _parse_minimal_yaml(raw)

    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML config: {exc}") from exc
    return loaded or {}


def _parse_minimal_yaml(raw: str) -> dict[str, Any]:
    lines: list[tuple[int, str]] = []
    for original in raw.splitlines():
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        indent = len(original) - len(original.lstrip(" "))
        lines.append((indent, original.strip()))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines) or lines[index][0] < indent:
            return {}, index
        if lines[index][0] == indent and lines[index][1].startswith("- "):
            return parse_list(index, indent)
        return parse_mapping(index, indent)

    def parse_list(index: int, indent: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while index < len(lines):
            current_indent, text = lines[index]
            if current_indent != indent or not text.startswith("- "):
                break
            item_text = text[2:].strip()
            index += 1
            if not item_text:
                child, index = parse_block(index, indent + 2)
                items.append(child)
            elif _looks_like_mapping_item(item_text):
                key, value = _split_key_value(item_text)
                item: dict[str, Any] = {}
                if value:
                    item[key] = _parse_scalar(value)
                else:
                    child, index = parse_block(index, indent + 2)
                    item[key] = child
                if index < len(lines) and lines[index][0] == indent + 2:
                    child, index = parse_mapping(index, indent + 2)
                    item.update(child)
                items.append(item)
            else:
                items.append(_parse_scalar(item_text))
        return items, index

    def parse_mapping(index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(lines):
            current_indent, text = lines[index]
            if current_indent < indent or current_indent != indent or text.startswith("- "):
                break
            key, value = _split_key_value(text)
            index += 1
            if value:
                result[key] = _parse_scalar(value)
            else:
                child, index = parse_block(index, indent + 2)
                result[key] = child
        return result, index

    parsed, _ = parse_block(0, lines[0][0] if lines else 0)
    return parsed if isinstance(parsed, dict) else {}


def _looks_like_mapping_item(text: str) -> bool:
    return ":" in text and not text.startswith(('"', "'"))


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"Expected key/value YAML line, got: {text}")
    key, value = text.split(":", 1)
    return key.strip().strip('"').strip("'"), value.strip()


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            inner = value[1:-1].strip()
            return [] if not inner else [part.strip().strip('"').strip("'") for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
