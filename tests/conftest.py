import pytest

from paramguard import meta, rules


@pytest.fixture(scope="session")
def px4():
    return meta.load("px4-1.15.4")


@pytest.fixture(scope="session")
def px4_rules(px4):
    rs = rules.load("builtin:px4")
    rules.validate_against(rs, px4)
    return rs
