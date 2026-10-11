from pathlib import Path

import pytest

from octop.infra.agents.workspace.enterprise import managed_content_path
from octop.infra.errors import OctopError


def test_managed_download_denies_another_workspace(tmp_path: Path):
    workspace = tmp_path / "employee-a"
    other = tmp_path / "employee-b" / "inbound" / "secret.pdf"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"private")
    with pytest.raises(OctopError):
        managed_content_path(workspace, str(other))


@pytest.mark.parametrize(
    "path",
    [
        "../employee-b/inbound/x.pdf",
        "file:///etc/passwd",
        "/home/other/inbound/secret.pdf",
        ".octop/memory.sqlite",
        "inbound/../../private.txt",
        "C:\\other\\secret.txt",
    ],
)
def test_managed_download_denies_unscoped_paths(tmp_path: Path, path: str):
    with pytest.raises(OctopError):
        managed_content_path(tmp_path / "workspace", path)


def test_managed_download_accepts_own_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    path = workspace / "inbound" / "document.pdf"
    assert managed_content_path(workspace, str(path)) == "inbound/document.pdf"
    assert managed_content_path(workspace, "/inbound/document.pdf") == "inbound/document.pdf"
