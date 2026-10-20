import json
import os
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

from binforge.logging_helper import logger
from binforge.utils import load_yaml

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is not None:
        return _client

    api_key_path = os.environ.get("BINFORGE_OPENAI_API_KEY_PATH", "").strip()
    if not api_key_path:
        raise RuntimeError(
            "BINFORGE_OPENAI_API_KEY_PATH is not set. "
            "BinForge buildfixer requires an explicit path to an API key file."
        )

    key_path = Path(api_key_path)
    if not key_path.is_file():
        raise RuntimeError(
            f"BINFORGE_OPENAI_API_KEY_PATH does not point to a readable file: {api_key_path}"
        )

    api_key = key_path.read_text(encoding="utf-8").strip()
    if not api_key:
        raise RuntimeError(
            f"API key file is empty: {api_key_path}"
        )

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = os.environ.get("BINFORGE_OPENAI_BASE_URL", "").strip()
    if base_url:
        client_kwargs["base_url"] = base_url

    _client = OpenAI(**client_kwargs)  # type: ignore[arg-type]
    return _client

DEFAULT_PROMPT_PATH = Path(__file__).resolve().with_name("prompt.txt")

JSONDict = dict[str, Any]
# DEFAULT_MODEL = "gpt-5.3-chat-latest"  # API support ended on 2026-08-10.
DEFAULT_MODEL = "gpt-5.3-codex"
SOLUTION_KEYS = ("deps", "options", "cflags", "ldflags")
DECISION_TO_KEY = {
    "deps": "deps",
    "dependency": "deps",
    "dependencies": "deps",
    "options": "options",
    "option": "options",
    "feature": "options",
    "configure_options": "options",
    "cflags": "cflags",
    "ldflags": "ldflags",
    "none": "none",
}


def load_prompt_template(path: Path = DEFAULT_PROMPT_PATH) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def render_build_prompt(
    template: str,
    *,
    project_name: str,
    project_version: str,
    phase: str,
    return_code: int | None,
    log_text: str,
    help_output: str,
    failed_solution_json: str,
    instructions: str = "",
) -> str:
    project_meta = f"{project_name}-{project_version}"
    instructions_block = instructions.strip() or "None"
    return_code_text = "unknown" if return_code is None else str(return_code)

    return (
        template
        .replace("<<PROJECT_METADATA>>", project_meta)
        .replace("<<PHASE>>", phase)
        .replace("<<RETURN_CODE>>", return_code_text)
        .replace("<<LOG_TEXT>>", log_text)
        .replace("<<HELP_OUTPUT>>", help_output)
        .replace("<<FAILED_SOLUTION_JSON>>", failed_solution_json)
        .replace("<<INSTRUCTIONS>>", instructions_block)
    )


def call_build_fix_model(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
) -> dict[str, Any]:
    request_kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }
    if temperature is not None:
        request_kwargs["temperature"] = temperature

    started_at = time.perf_counter()
    response = get_client().responses.create(**request_kwargs)
    request_ms = int((time.perf_counter() - started_at) * 1000)

    usage_obj = getattr(response, "usage", None)
    usage: dict[str, Any] = {}
    if usage_obj is not None:
        for key in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens"):
            value = getattr(usage_obj, key, None)
            if value is not None:
                usage[key] = value
        input_tokens_details = getattr(usage_obj, "input_tokens_details", None)
        if input_tokens_details is not None:
            cached_tokens = getattr(input_tokens_details, "cached_tokens", None)
            if cached_tokens is not None:
                usage["cached_input_tokens"] = cached_tokens

    if hasattr(response, "output_text") and response.output_text is not None:
        return {
            "text": response.output_text,
            "usage": usage,
            "request_ms": request_ms,
            "model": model,
        }

    try:
        first_output = response.output[0]
        first_content = first_output.content[0]  # type: ignore[index]
        return {
            "text": first_content.text,
            "usage": usage,
            "request_ms": request_ms,
            "model": model,
        }
    except Exception as e:
        logger.error(f"[OpenAI] Unexpected response structure: {e!r}")
        raise


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Expected list, got {type(value)}")

    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append(text)
            continue
        if isinstance(item, dict) and "name" in item:
            text = str(item["name"]).strip()
            if text:
                out.append(text)
            continue
        raise ValueError(f"Unsupported solution item type: {type(item)}")
    return out


def _normalize_dep_item(item: Any) -> str | dict[str, Any]:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            raise ValueError("Dependency name is empty")
        return text

    if not isinstance(item, dict):
        raise ValueError(f"Unsupported dependency item type: {type(item)}")

    name = str(item.get("name", "")).strip()
    if not name:
        raise ValueError("Dependency object must include a non-empty name")

    source = item.get("source")
    if source is None:
        normalized: dict[str, Any] = {"name": name}
        version = str(item.get("version", "")).strip()
        if version:
            normalized["version"] = version
        return normalized

    if not isinstance(source, dict):
        raise ValueError("Dependency source must be an object")

    source_type = str(source.get("type", "")).strip()
    source_value = str(source.get("value", "")).strip()
    if not source_type and not source_value:
        normalized = {"name": name}
        version = str(item.get("version", "")).strip()
        if version:
            normalized["version"] = version
        return normalized

    if source_type not in ("apt", "url", "url_git"):
        raise ValueError(f"Unsupported dependency source type: {source_type}")
    if not source_value:
        raise ValueError("Dependency source.value is empty")

    normalized: dict[str, Any] = {
        "name": name,
        "source": {
            "type": source_type,
            "value": source_value,
        },
    }

    version = str(item.get("version", "")).strip()
    if version:
        normalized["version"] = version

    return normalized


def _normalize_dep_list(value: Any) -> list[str | dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Expected list, got {type(value)}")
    return [_normalize_dep_item(item) for item in value]


def _normalize_solution(result: JSONDict) -> dict[str, list[Any]]:
    raw_solution = result.get("solution", {})
    if not isinstance(raw_solution, dict):
        raise ValueError("solution must be an object")

    solution = {
        "deps": _normalize_dep_list(
            raw_solution.get("deps", result.get("deps", raw_solution.get("dependencies", result.get("dependencies"))))
        ),
        "options": _normalize_string_list(
            raw_solution.get("options", result.get("options", raw_solution.get("configure_options", result.get("configure_options"))))
        ),
        "cflags": _normalize_string_list(raw_solution.get("cflags", result.get("cflags"))),
        "ldflags": _normalize_string_list(raw_solution.get("ldflags", result.get("ldflags"))),
    }
    return solution


def _normalize_decision_type(result: JSONDict, solution: dict[str, list[str]]) -> tuple[str, str]:
    reason = ""
    decision_type = ""

    decision = result.get("decision")
    if isinstance(decision, dict):
        decision_type = str(decision.get("type", "")).strip().lower()
        reason = str(decision.get("reason", "")).strip()

    classification = result.get("classification")
    if not decision_type and isinstance(classification, dict):
        truthy = [key for key, value in classification.items() if value]
        if len(truthy) == 1:
            decision_type = truthy[0].strip().lower()
        elif len(truthy) == 0:
            decision_type = "none"
        else:
            raise ValueError(f"Multiple classification types set: {truthy}")

    decision_type = DECISION_TO_KEY.get(decision_type, decision_type)

    non_empty = [key for key in SOLUTION_KEYS if solution[key]]

    if not decision_type:
        if not non_empty:
            decision_type = "none"
        elif len(non_empty) == 1:
            decision_type = non_empty[0]
        else:
            raise ValueError(f"Multiple solution fields are non-empty: {non_empty}")

    if decision_type not in (*SOLUTION_KEYS, "none"):
        raise ValueError(f"Unsupported decision type: {decision_type}")

    if decision_type == "none":
        if non_empty:
            raise ValueError("decision.type is none but solution is not empty")
        return decision_type, reason

    if non_empty != [decision_type]:
        raise ValueError(
            f"decision.type={decision_type} but non-empty solution fields are {non_empty}"
        )

    return decision_type, reason


def normalize_model_output(
    result: JSONDict,
    *,
    project_name: str,
    project_version: str,
) -> JSONDict:
    if not isinstance(result, dict):
        raise ValueError("Model output is not a JSON object")

    solution = _normalize_solution(result)
    decision_type, reason = _normalize_decision_type(result, solution)

    project = result.get("project", {})
    if not isinstance(project, dict):
        project = {}

    normalized = {
        "project": {
            "name": str(project.get("name", project_name)),
            "version": str(project.get("version", project_version)),
        },
        "decision": {
            "type": decision_type,
            "reason": reason,
        },
        "solution": solution,
    }
    return normalized


def analyze_build_error_once(
    *,
    prompt_path: Path | None,
    project_name: str,
    project_version: str,
    phase: str,
    error_log_path: Path,
    build_options_help_path: Path,
    failed_solution_json: str | None = None,
    instructions: str = "",
    model: str = DEFAULT_MODEL,
    temperature: float | None = None,
    return_code: int | None = None,
) -> JSONDict:
    template_path = prompt_path or DEFAULT_PROMPT_PATH
    template = load_prompt_template(template_path)

    try:
        with error_log_path.open("r", encoding="utf-8", errors="replace") as f:
            log_text = f.read()
    except FileNotFoundError:
        logger.error(f"[LLM] error_log_path not found: {error_log_path}")
        raise

    try:
        with build_options_help_path.open("r", encoding="utf-8", errors="replace") as f:
            build_options_help = f.read()
    except FileNotFoundError:
        logger.error(f"[LLM] build_options_help_path not found: {build_options_help_path}")
        raise

    if not failed_solution_json or not failed_solution_json.strip():
        default_solution = {
            "project": {
                "name": project_name,
                "version": project_version,
            },
            "decision": {
                "type": "none",
                "reason": "No previous solution applied.",
            },
            "solution": {
                "deps": [],
                "options": [],
                "cflags": [],
                "ldflags": [],
            },
        }
        failed_solution_json = json.dumps(default_solution, ensure_ascii=False, indent=2)

    prompt = render_build_prompt(
        template,
        project_name=project_name,
        project_version=project_version,
        phase=phase,
        return_code=return_code,
        log_text=log_text,
        help_output=build_options_help,
        failed_solution_json=failed_solution_json,
        instructions=instructions,
    )

    model_result = call_build_fix_model(
        prompt,
        model=model,
        temperature=temperature,
    )
    raw_output = str(model_result.get("text", ""))

    try:
        result: JSONDict = json.loads(raw_output)
    except json.JSONDecodeError as e:
        logger.error("[LLM] Failed to parse JSON from model output")
        logger.error(f"[LLM] Raw output:\n{raw_output}")
        raise e

    normalized = normalize_model_output(
        result,
        project_name=project_name,
        project_version=project_version,
    )
    normalized["_llm_meta"] = {
        "model": model_result.get("model", model),
        "request_ms": model_result.get("request_ms", 0),
        "usage": model_result.get("usage", {}),
    }
    return normalized
