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

    # Popups: close cookie banners / Google One Tap / "sign in to save" boxes before steps (0 tokens).
    dismiss_popups: bool = field(default_factory=lambda: _env("BE_DISMISS_POPUPS", "1") != "0")
    popup_wait_ms: int = 600  # popups usually appear just after a page loads

    # Bot checks: how long one tool call waits for the user to solve a captcha before handing
    # back needs_human. Keep under the ~60s Cowork device-call timeout.
    human_wait_s: int = field(default_factory=lambda: int(_env("BE_HUMAN_WAIT", "30")))

    @property
    def cache_path(self):
        return os.path.join(self.data_dir, "cache.sqlite")

    @property
    def log_path(self):
        return os.path.join(self.data_dir, "steps.jsonl")

    @property
    def popups_path(self):
        return os.path.join(self.data_dir, "popups.json")

    @property
    def profile_dir(self):
        return os.path.join(self.data_dir, "profile")
