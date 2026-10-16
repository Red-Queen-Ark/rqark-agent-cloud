from collections.abc import Callable, Sequence

from rqark_core.types.prompt import BasePrompt

from src.apps.sandbox.prompts.agents_md import AgentsMdPrompt
from src.apps.sandbox.prompts.git_project import GitProjectPrompt
from src.apps.sandbox.prompts.platform import PlatformPrompt
from src.apps.sandbox.prompts.workspace import WorkspacePrompt
from src.apps.sandbox.workspace import Workspace

__all__ = ['AgentsMdPrompt', 'GitProjectPrompt', 'PROMPTS', 'PlatformPrompt', 'WorkspacePrompt', 'swap_prompts']

PROMPTS: dict[str, Callable[[Workspace], BasePrompt]] = {'AgentsMdPrompt': AgentsMdPrompt, 'GitProjectPrompt': GitProjectPrompt, 'PlatformPrompt': lambda workspace: PlatformPrompt(), 'WorkspacePrompt': WorkspacePrompt}


def swap_prompts(prompts: Sequence[BasePrompt], workspace: Workspace) -> list[BasePrompt]:
    return [_swap(prompt, workspace) for prompt in prompts]


def _swap(prompt: BasePrompt, workspace: Workspace) -> BasePrompt:
    replacement = PROMPTS.get(type(prompt).__name__)
    if replacement is None:
        return prompt
    return replacement(workspace)
