from rqark_core.types.tool import BaseTool

from src.apps.sandbox.tools.git.base_git import SandboxGitTool
from src.apps.sandbox.tools.git.gitea import Gitea
from src.apps.sandbox.tools.git.github import GitHub
from src.apps.sandbox.tools.git.gitlab import GitLab
from src.apps.sandbox.workspace import Workspace

GIT_TOOLS: dict[str, type[SandboxGitTool]] = {'Gitea': Gitea, 'GitHub': GitHub, 'GitLab': GitLab}

__all__ = ['GIT_TOOLS', 'Gitea', 'GitHub', 'GitLab', 'SandboxGitTool', 'build_git_tool']


def build_git_tool(workspace: Workspace, name: str, params: dict | None = None) -> BaseTool:
    return GIT_TOOLS[name](workspace, **(params or {}))
