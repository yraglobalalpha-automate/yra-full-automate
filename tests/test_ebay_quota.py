"""ebay_calls_remaining (2026-10-01): the sync sizes its batch to eBay's own
remaining-calls figure. Extracted via AST - generate_xml.py imports the whole
pipeline."""
import ast
import io
import logging
from pathlib import Path

SRC = io.open(Path(__file__).resolve().parents[1] / "generate_xml.py", encoding="utf-8").read()


def _load(requests_stub):
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "ebay_calls_remaining")
    ns = {"requests": requests_stub, "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "quota_extract", "exec"), ns)
    return ns["ebay_calls_remaining"]


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def stub(resp=None, exc=None):
    class R:
        @staticmethod
        def get(*a, **k):
            if exc:
                raise exc
            return resp
    return R


BODY = {"rateLimits": [{"resources": [
    {"name": "buy.browse", "rates": [{"timeWindow": 86400, "limit": 5000, "remaining": 3180}]},
    {"name": "buy.browse.item.bulk", "rates": [{"timeWindow": 86400, "limit": 5000, "remaining": 5000}]},
]}]}


def test_reads_the_browse_daily_remaining_not_the_bulk_pool():
    assert _load(stub(FakeResp(200, BODY)))("tok") == 3180


def test_unreadable_answers_give_none_never_an_exception():
    assert _load(stub(FakeResp(500, {})))("tok") is None
    assert _load(stub(FakeResp(200, {"rateLimits": []})))("tok") is None
    assert _load(stub(exc=RuntimeError("boom")))("tok") is None
