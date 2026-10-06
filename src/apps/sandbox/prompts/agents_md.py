import shlex

from rqark_core.types.prompt import BasePrompt

from src.apps.sandbox.workspace import Workspace

AGENTS_MD = 'AGENTS.md'


class AgentsMdPrompt(BasePrompt):
    priority = 50

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def get_messages(self) -> str | None:
        path = f'{self.workspace.WORKDIR}/{AGENTS_MD}'
        result = self.workspace.run_shell(f'cat -- {shlex.quote(path)}')
        if result.exit_code != 0:
            return None
        return f'## Project Instructions (AGENTS.md)\n\n{result.stdout.strip()}'
