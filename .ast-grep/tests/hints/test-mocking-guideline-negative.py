# ruff: noqa
# pyright: reportMissingImports=false, reportUndefinedVariable=false
# pylint: skip-file
"""Negative test cases for test-mocking-guideline hint rule.

These examples SHOULD NOT trigger the hint (no mocks used).
"""


# Should NOT trigger: Using real objects
def test_with_real_object():
    service = RealService()
    result = service.do_something()


# Should NOT trigger: Using fixtures
async def test_with_fixture(db_context):
    result = await db_context.fetch_one("SELECT 1")


# Should NOT trigger: No mocks at all
def test_simple_assertion():
    assert 1 + 1 == 2


# Should NOT trigger: Using fake implementation
class FakeTelegramClient:
    def send_message(self, msg):
        pass


def test_with_fake():
    client = FakeTelegramClient()
    client.send_message("test")
