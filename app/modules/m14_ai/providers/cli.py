"""CLI provider (spec 7.1, D-08): runs a locally installed AI CLI on the backend host.

Safety rules, all enforced here:
- only a command key listed in ``AI_CLI_ALLOWLIST`` runs (checked at construction);
- ``asyncio.create_subprocess_exec`` with an argument list, never a shell string, and the
  executable is looked up by key, so request data never reaches argv;
- the prompt goes on stdin;
- hard timeout (kills the whole process group), output size cap;
- sanitized environment (no application secrets) and a temporary working directory.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile

from app.core.config import settings
from app.core.errors import AppError
from app.modules.m14_ai.providers.base import parse_json_output, validate_schema

log = logging.getLogger("app.ai")

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_OUTPUT_BYTES = 1_000_000

# Fixed argument lists per known command key; unknown allowlisted keys run with no arguments.
_KNOWN_ARGS: dict[str, list[str]] = {"claude": ["-p", "--output-format", "text"]}
_MODEL_FLAG: dict[str, str] = {"claude": "--model"}
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
# The only variables passed to the child. Secrets (DB URL, JWT keys, API keys) are never copied.
_ENV_KEEP = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def sanitized_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}


def ensure_allowed(command_key: str | None) -> str:
    """Return the command key if it is allowlisted; ``AI_NOT_CONFIGURED`` otherwise."""
    allowed = {item.strip() for item in settings.ai_cli_allowlist}
    if not command_key or not _KEY.match(command_key) or command_key not in allowed:
        raise AppError("AI_NOT_CONFIGURED", details={"reason": "cli_command_not_allowed"})
    return command_key


class CliProvider:
    name = "cli"

    def __init__(self, *, command_key: str | None, model: str | None = None):
        self.command_key = ensure_allowed(command_key)
        self.model = model or ""
        self.last_usage = None

    def __repr__(self) -> str:
        return f"CliProvider(command_key={self.command_key!r})"

    def _argv(self, executable: str) -> list[str]:
        argv = [executable, *_KNOWN_ARGS.get(self.command_key, [])]
        flag = _MODEL_FLAG.get(self.command_key)
        if flag and self.model and _MODEL_ID.match(self.model):
            argv += [flag, self.model]
        return argv

    async def generate_json(
        self, *, system: str, prompt: str, json_schema: dict, max_tokens: int, timeout_sec: int
    ) -> dict:
        env = sanitized_env()
        executable = shutil.which(self.command_key, path=env.get("PATH"))
        if executable is None:
            raise AppError("AI_PROVIDER_ERROR", details={"reason": "cli_not_installed"})
        full_prompt = (
            f"{system}\n\n{prompt}\n\nReturn only one JSON object matching this JSON Schema, "
            f"with no other text:\n{json.dumps(json_schema)}\n"
        )
        timeout = max(1, min(timeout_sec, settings.ai_cli_timeout_sec))
        with tempfile.TemporaryDirectory(prefix="ai-cli-") as workdir:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._argv(executable),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise AppError("AI_PROVIDER_ERROR", details={"reason": "cli_start_failed"}) from exc
            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(full_prompt.encode("utf-8")), timeout=timeout
                )
            except TimeoutError as exc:
                self._kill(proc)
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise AppError("AI_PROVIDER_ERROR", details={"reason": "timeout"}) from exc
        if proc.returncode != 0:
            log.warning("ai cli exited code=%s key=%s", proc.returncode, self.command_key)
            raise AppError(
                "AI_PROVIDER_ERROR", details={"reason": "cli_failed", "exit_code": proc.returncode}
            )
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise AppError("AI_OUTPUT_INVALID", details={"reason": "too_large"})
        result = parse_json_output(stdout.decode("utf-8", errors="replace"))
        validate_schema(result, json_schema)
        return result

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def test_connection(self) -> bool:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        try:
            await self.generate_json(
                system="Reply with JSON only.",
                prompt='Return {"ok": true}.',
                json_schema=schema,
                max_tokens=256,
                timeout_sec=30,
            )
        except AppError as exc:
            return exc.code == "AI_OUTPUT_INVALID"
        return True
