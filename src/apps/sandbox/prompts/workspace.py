from rqark_core.types.prompt import BasePrompt

from src.apps.sandbox.workspace import Workspace


class WorkspacePrompt(BasePrompt):
    priority = 40

    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def get_messages(self) -> str:
        return f'## Workspace\nCurrent workspace: {self.workspace.WORKDIR}'
