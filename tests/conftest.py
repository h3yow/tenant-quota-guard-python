import logging

import pytest


@pytest.fixture(autouse=True)
def _log_to_stdout(caplog):
    """单测日志输出请求输入与判定依据。"""
    caplog.set_level(logging.INFO, logger="quota_guard")
    yield
