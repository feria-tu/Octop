"""Legacy message processing remains compatible with existing row doubles."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from octop.infra.gateway.process.processor import GlobalProcessor


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [SimpleNamespace(user_id=1), MagicMock(user_id=1)])
async def test_legacy_row_without_enterprise_kind_skips_enterprise_guard(row: object) -> None:
    processor = GlobalProcessor.__new__(GlobalProcessor)
    processor._user_repo = MagicMock()
    msg = SimpleNamespace(channel_type="telegram")

    assert await processor._enterprise_message_allowed(msg, row, 1) is True
    processor._user_repo.get.assert_not_called()
