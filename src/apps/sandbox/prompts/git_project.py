import shlex

from rqark_core.types.prompt import BasePrompt, PromptContext

from src.apps.sandbox.workspace import Workspace


class GitProjectPrompt(BasePrompt):
    priority = 70

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def _git(self, args: list[str]) -> str | None:
        result = self.workspace.run_shell(shlex.join(['git', *args]), cwd=self.workspace.WORKDIR, timeout=10)
        if result.exit_code != 0:
            return None
        return result.stdout.strip() or None

    async def get_messages(self, ctx: PromptContext | None = None) -> str | None:
        if self.workspace.run_shell(f'[ -d {shlex.quote(self.workspace.WORKDIR + "/.git")} ]').exit_code != 0:
            return None

        parts: list[str] = ['## Git Project Context']

        branch = self._git(['branch', '--show-current'])
        if branch:
            parts.append(f'Current branch: {branch}')

        head = self._git(['rev-parse', 'HEAD'])
        if head:
            parts.append(f'HEAD: {head}')

        status = self._git(['status', '--porcelain'])
        if status:
            status_lines = status.split('\n')
            parts.append('Working tree status (porcelain):\n' + '\n'.join(status_lines[:40]))
            if len(status_lines) > 40:
                parts.append(f'... ({len(status_lines) - 40} more lines truncated)')
        else:
            parts.append('Working tree: clean')

        log = self._git(['log', '--oneline', '-10', '--decorate'])
        if log:
            parts.append(f'Recent commits:\n{log}')

        return '\n\n'.join(parts)
