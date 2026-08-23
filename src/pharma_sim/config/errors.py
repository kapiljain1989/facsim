"""Configuration error types.

Config errors are the primary interface between the person editing YAML and the
engine, so they carry the file, the YAML path and a suggested fix rather than a
bare stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    """A single problem found in configuration."""

    file: str
    path: str
    message: str
    hint: str = ""

    def render(self) -> str:
        location = f"{self.file}:{self.path}" if self.path else self.file
        text = f"  {location}\n      {self.message}"
        if self.hint:
            text += f"\n      hint: {self.hint}"
        return text


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or fails cross-file linting."""

    def __init__(self, issues: list[ConfigIssue], headline: str = "invalid configuration") -> None:
        self.issues = issues
        self.headline = headline
        body = "\n".join(issue.render() for issue in issues)
        super().__init__(f"{headline} ({len(issues)} issue(s)):\n{body}")


@dataclass
class IssueCollector:
    """Accumulates issues so one run reports every problem, not just the first."""

    issues: list[ConfigIssue] = field(default_factory=list)

    def add(self, file: str, path: str, message: str, hint: str = "") -> None:
        self.issues.append(ConfigIssue(file=file, path=path, message=message, hint=hint))

    def raise_if_any(self, headline: str) -> None:
        if self.issues:
            raise ConfigError(self.issues, headline)

    def __bool__(self) -> bool:
        return bool(self.issues)
