from rqark_core.types.tool import BaseTool

from src.apps.sandbox.tools.base import WorkspaceTool
from src.apps.sandbox.tools.edit_file import EditFile
from src.apps.sandbox.tools.exec import Exec
from src.apps.sandbox.tools.grep import Grep
from src.apps.sandbox.tools.list_dir import ListDir
from src.apps.sandbox.tools.read_file import ReadFile
from src.apps.sandbox.tools.write_file import WriteFile
from src.apps.sandbox.workspace import Workspace

__all__ = ['EditFile', 'Exec', 'Grep', 'ListDir', 'ReadFile', 'WorkspaceTool', 'WriteFile', 'build_workspace_tools']


def build_workspace_tools(workspace: Workspace) -> list[BaseTool]:
    return [Exec(workspace), ReadFile(workspace), WriteFile(workspace), EditFile(workspace), Grep(workspace), ListDir(workspace)]
