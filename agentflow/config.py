"""Configuration loading.

Two sources, kept deliberately separate:

* **Settings** (env vars) -- credentials and endpoints. Never committed.
* **Config files** (``config/*.yaml``) -- the asset registry, the rule set and
  the agent graph. These are business logic, versioned in git and reviewable
  by the people who own the policy rather than by engineers.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
KNOWLEDGE_DIR = ROOT / "knowledge"
WEB_DIR = ROOT / "web"
SAMPLES_DIR = ROOT / "samples"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Settings:
    """Runtime settings, read from the environment.

    Every Azure service is optional. When a key is absent the corresponding
    adapter falls back to a deterministic local implementation so the whole
    pipeline still runs end to end -- that is what makes the demo work on a
    laptop with no cloud account, and what makes the tests hermetic.
    """

    def __init__(self) -> None:
        self.llm_provider = _env("AGENTFLOW_LLM_PROVIDER", "simulated").lower()

        self.azure_openai_endpoint = _env("AZURE_OPENAI_ENDPOINT")
        self.azure_openai_key = _env("AZURE_OPENAI_API_KEY")
        self.azure_openai_deployment = _env("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        self.azure_openai_api_version = _env("AZURE_OPENAI_API_VERSION", "2024-06-01")

        self.openai_key = _env("OPENAI_API_KEY")
        self.openai_model = _env("OPENAI_MODEL", "gpt-4o-mini")

        self.docint_endpoint = _env("AZURE_DOCINT_ENDPOINT")
        self.docint_key = _env("AZURE_DOCINT_API_KEY")

        self.language_endpoint = _env("AZURE_LANGUAGE_ENDPOINT")
        self.language_key = _env("AZURE_LANGUAGE_API_KEY")

        self.translator_endpoint = _env(
            "AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com"
        )
        self.translator_key = _env("AZURE_TRANSLATOR_API_KEY")
        self.translator_region = _env("AZURE_TRANSLATOR_REGION")

        self.content_safety_endpoint = _env("AZURE_CONTENT_SAFETY_ENDPOINT")
        self.content_safety_key = _env("AZURE_CONTENT_SAFETY_API_KEY")

        self.search_endpoint = _env("AZURE_SEARCH_ENDPOINT")
        self.search_key = _env("AZURE_SEARCH_API_KEY")
        self.search_index = _env("AZURE_SEARCH_INDEX", "agentflow-manuals")

        self.power_automate_url = _env("POWER_AUTOMATE_WEBHOOK_URL")
        self.teams_webhook_url = _env("TEAMS_WEBHOOK_URL")
        self.erp_base_url = _env("ERP_API_BASE_URL")
        self.erp_api_key = _env("ERP_API_KEY")

        self.api_key = _env("AGENTFLOW_API_KEY")
        # Purely cosmetic: a real run finishes in ~30ms, which is invisible.
        # Slowing the graph down makes the pipeline legible on screen. Set to 0
        # for benchmarks, load tests and CI.
        self.step_delay_ms = int(_env("AGENTFLOW_STEP_DELAY_MS", "320") or 0)
        self.working_language = _env("AGENTFLOW_WORKING_LANGUAGE", "en")

    @property
    def azure_openai_ready(self) -> bool:
        return bool(self.azure_openai_endpoint and self.azure_openai_key)

    @property
    def openai_ready(self) -> bool:
        return bool(self.openai_key)

    def service_status(self) -> dict[str, dict[str, Any]]:
        """What the dashboard shows in its 'Azure services' panel."""
        def row(name: str, live: bool, purpose: str) -> dict[str, Any]:
            return {"service": name, "mode": "live" if live else "simulated", "purpose": purpose}

        return {
            "openai": row("Azure OpenAI", self.azure_openai_ready, "Diagnosis, asset matching, planning"),
            "document_intelligence": row("Azure AI Document Intelligence", bool(self.docint_key), "Read scanned inspection reports"),
            "language": row("Azure AI Language", bool(self.language_key), "PII detection and redaction"),
            "translator": row("Azure AI Translator", bool(self.translator_key), "Multilingual operator reports"),
            "content_safety": row("Azure AI Content Safety", bool(self.content_safety_key), "Input and output guardrails"),
            "search": row("Azure AI Search", bool(self.search_key), "Manual retrieval for grounding"),
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=8)
def load_yaml(name: str) -> dict[str, Any]:
    """Load and cache a config file from ``config/``."""
    path = CONFIG_DIR / name
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def assets_config() -> dict[str, Any]:
    return load_yaml("assets.yaml")


def rules_config() -> dict[str, Any]:
    return load_yaml("rules.yaml")


def pipeline_config() -> dict[str, Any]:
    return load_yaml("pipeline.yaml")


def prompts_config() -> dict[str, Any]:
    return load_yaml("prompts.yaml")


def reload_config() -> None:
    """Drop the config cache so edits are picked up without a restart."""
    load_yaml.cache_clear()
