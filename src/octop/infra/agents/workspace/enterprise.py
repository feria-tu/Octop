"""Private-content path policy for company-managed assistants."""

from pathlib import Path, PurePosixPath

from octop.infra.errors import ErrorCode, OctopError


def managed_content_path(workspace_dir: Path, raw: str) -> str:
    root = workspace_dir.resolve()
    normalized = raw.replace("\\", "/")
    if normalized.startswith(("/inbound/", "/artifacts/", "/outbound/")):
        normalized = normalized.lstrip("/")
    candidate = Path(normalized)
    if candidate.is_absolute():
        try:
            normalized = candidate.resolve().relative_to(root).as_posix()
        except ValueError:
            if normalized.startswith(("/inbound/", "/artifacts/", "/outbound/")):
                normalized = normalized.lstrip("/")
            else:
                raise OctopError.localized(ErrorCode.FORBIDDEN) from None
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or parts[0] not in {"inbound", "artifacts", "outbound"}
        or any(p.startswith(".") or ":" in p for p in parts)
    ):
        raise OctopError.localized(ErrorCode.FORBIDDEN)
    try:
        (root / normalized).resolve().relative_to(root)
    except ValueError:
        raise OctopError.localized(ErrorCode.FORBIDDEN) from None
    return PurePosixPath(normalized).as_posix()
