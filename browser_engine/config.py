import os
from dataclasses import dataclass, field


def _env(name, default):
    return os.getenv(name, default)


@dataclass
class Config:
    # Tier 1: local model via any OpenAI-compatible server (Ollama, LM Studio, MLX server)
    local_base_url: str = field(default_factory=lambda: _env("BE_LOCAL_BASE_URL", "http://localhost:11434/v1"))
    local_model: str = field(default_factory=lambda: _env("BE_LOCAL_MODEL", "qwen2.5:7b"))

    # Tiers 2-3 + planner: Claude via Anthropic API (needs ANTHROPIC_API_KEY)
    claude_text_model: str = field(default_factory=lambda: _env("BE_CLAUDE_TEXT_MODEL", "claude-haiku-5-5"))
    claude_vision_model: str = field(default_factory=lambda: _env("BE_CLAUDE_VISION_MODEL", "claude-sonnet-5-5"))
    claude_planner_model: str = field(default_factory=lambda: _env("BE_CLAUDE_PLANNER_MODEL", "claude-sonnet-5-5"))

    data_dir: str = field(default_factory=lambda: os.path.expanduser(_env("BE_DATA_DIR", "~/.browser_engine")))

    # Escalation: how many attempts each tier gets before handing up
    local_tries: int = 2
    claude_text_tries: int = 2
    vision_tries: int = 1

    # Hard cap on Claude tokens per run (planner + escalations)
    max_claude_tokens: int = 60_000

    # Observation size: max interactive elements sent to a model
    max_elements: int = 150

    # Wait after each action for the page to settle (ms)
    settle_ms: int = 800

    @property
    def cache_path(self):
        return os.path.join(self.data_dir, "cache.sqlite")

    @property
    def log_path(self):
        return os.path.join(self.data_dir, "steps.jsonl")

    @property
    def profile_dir(self):
        return os.path.join(self.data_dir, "profile")
