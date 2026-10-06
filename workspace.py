from typing import cast

from docker.errors import APIError, NotFound

import docker

client  = docker.from_env()

class WorkSpace:
    def __init__(self, user_id: str, username: str):
        self.user_id: str = user_id
        self.user_name: str = username
        self.prefix: str = 'rqark-user'
        self.image = 'ubuntu:24.04'
        self.mem_limit: str = '1g'
        self.cpu_quota: int = 20000

    @property
    def _name(self):
        return f"{self.prefix}-{self.user_name}-{self.user_id}"

    @property
    def _container_name(self):
        return f"{self._name}-workspace"

    def volume(self):
        name = f"{self.prefix}-{self.user_name}-{self.user_id}"
        try:
            return client.volumes.get(name)
        except NotFound:
            return client.volumes.create(name)

    def container(self):
        try:
            return client.containers.get(self._container_name)
        except NotFound:
            return client.containers.run(
                self.image,
                command="sleep infinity",          # 保持容器存活
                name=self._container_name,
                detach=True,                        # 后台运行
                mem_limit=self.mem_limit,
                cpu_quota=self.cpu_quota,
                volumes={self.volume().name: {"bind": "/workspace", "mode": "rw"}},
                working_dir="/workspace",
                tty=True,
                stdin_open=True,
            )

    def exec(self, cmd: str):
        """在已有容器里执行命令，返回 (exit_code, stdout, stderr)"""
        container = self.container()
        try:
            exit_code, output = container.exec_run(
                cmd=["/bin/sh", "-c", cmd],         # 显式 shell，行为可控
                demux=True,                          # 分离 stdout / stderr
                workdir="/workspace",
            )
            stdout, stderr = cast("tuple[bytes | None, bytes | None]", output)
            return (
                exit_code,
                (stdout or b"").decode("utf-8", errors="replace"),
                (stderr or b"").decode("utf-8", errors="replace"),
            )
        except APIError as e:
            return (-1, "", str(e))

    def remove(self):
        try:
            self.container().remove(force=True)
        except NotFound:
            pass
        try:
            self.volume().remove(force=True)
        except (NotFound, APIError):
            pass

if __name__ == '__main__':
    work_space = WorkSpace("test", 'ansoncar')
    code, out, err = work_space.exec("apt-get update && apt-get install -y python3")
    print("exit code:", code)
    print("stdout:", out)
    print("stderr:", err)
    code, out, err = work_space.exec("python3 --version")
    print(out)
