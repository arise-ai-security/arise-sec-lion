from pathlib import Path
from uuid import uuid4

from plugins.security.docker_runtime import DockerSecBenchRuntime
from plugins.security.container_runtime import (
    SecBenchContainerSession,
    SecBenchWorkspace,
)


def test_exec_helper_falls_back_to_root_when_project_dirs_disappear(
    tmp_path: Path,
) -> None:
    workspace = SecBenchWorkspace(
        root_id=uuid4(),
        image="secb-tools:demo",
        host_root=tmp_path,
        host_source_dir=tmp_path / "src",
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )
    session = SecBenchContainerSession(
        workspace=workspace,
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image=workspace.image,
    )

    runtime = DockerSecBenchRuntime()
    runtime._write_exec_helper(session)

    script = workspace.helper_script.read_text(encoding="utf-8")
    assert 'docker exec "$CONTAINER" test -d /src 2>/dev/null' in script
    assert 'CMD="umask 000; $*"' in script
    assert 'docker exec -i -w / "$CONTAINER" bash -lc "$CMD"' in script
