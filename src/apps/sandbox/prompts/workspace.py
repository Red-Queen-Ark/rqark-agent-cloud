from rqark_core.types.prompt import BasePrompt, PromptContext

from src.apps.sandbox.workspace import Workspace


class WorkspacePrompt(BasePrompt):
    priority = 40

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    async def get_messages(self, ctx: PromptContext | None = None) -> str:
        return f'## Workspace\nCurrent workspace: {self.workspace.WORKDIR}'
