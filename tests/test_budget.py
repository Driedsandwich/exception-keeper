"""Spending admission and accounting tests with an injectable JST clock."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from sheetaudit.budget import Budget, BudgetStopped, FileLedger, JST, MemoryLedger, estimate_tokens, price
from sheetaudit.llm.client import ChatResult, LLMError, MockChatClient
from sheetaudit.llm.models import NANO, SUPER, ULTRA
from sheetaudit.review import review

ROOT = Path(__file__).resolve().parents[1]
DAY = '2026-10-24'
NOW = lambda: datetime(2026, 10, 24, 12, tzinfo=JST)


def client(usage=None, count=1):
    return MockChatClient([dict(content='{}', usage=usage)] * count)


def call(budget, chat, lane='public', model=NANO, max_tokens=100):
    return budget.complete(chat, 'test', 'ek', lane, model, [], max_tokens=max_tokens)


def test_prices_and_cached_input():
    assert price(NANO, 1_000_000, 1_000_000) == Decimal('0.30')
    assert price(SUPER, 1_000_000, 1_000_000) == Decimal('1.20')
    assert price(ULTRA, 1_000_000, 1_000_000) == Decimal('4.00')
    assert price(SUPER, 1_000_000, 0, 1_000_000) == Decimal('0.06')
    assert price(ULTRA, 1_000_000, 0, 1_000_000) == Decimal('0.12')


def test_reported_usage_and_cached_usage():
    budget = Budget(now=NOW)
    call(budget, client({'prompt_tokens': 1000, 'completion_tokens': 100,
                        'prompt_tokens_details': {'cached_tokens': 800}}), model=SUPER)
    report = budget.report()
    assert report['total_usd'] == pytest.approx(0.000198)
    assert report['usage'] == 'reported'
    assert report['stages']['test']['prompt_tokens'] == 1000


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': True, 'completion_tokens': 2},
                                   {'prompt_tokens': -1, 'completion_tokens': 2}])
def test_missing_or_malformed_usage_is_estimated(usage):
    budget = Budget(now=NOW)
    call(budget, client(usage))
    assert budget.report()['usage'] == 'estimated'
    assert budget.report()['total_usd'] > 0
    assert budget.report()['stages']['test']['completion_tokens'] == estimate_tokens(dict(content='{}', tool_calls=[]))


def test_per_run_cap_admits_worst_case_before_call():
    budget = Budget(now=NOW, run_cap=0.001)
    chat = client({'prompt_tokens': 500, 'completion_tokens': 500}, count=2)
    call(budget, chat, model=SUPER, max_tokens=1000)
    with pytest.raises(BudgetStopped, match='Per-run'):
        call(budget, chat, model=SUPER, max_tokens=1000)
    assert len(chat.requests) == 1


def test_pipeline_stops_mid_run_and_points_to_precomputed():
    script = json.loads((ROOT/'tests/fixtures/mock_scripts/club_membership_fees.json').read_text())
    chat = MockChatClient(script)
    report = review(ROOT/'samples/club_membership_fees.xlsx', chat,
                    Budget(now=NOW, run_cap=0.0025))
    reviewed = [f for f in report['findings'] if f['decided_by'] != 'fallback']
    remaining = [f for f in report['findings'] if f['verdict'] == 'not reviewed (budget)']
    assert reviewed and remaining
    assert report['stopped_reason'] and report['cost']['total_usd'] <= 0.0025
    assert report['precomputed_sample'] == 'samples/precomputed/club_membership_fees.json'
    assert not any(r['model'] == ULTRA for r in chat.requests)


def test_build_period_cap():
    ledger = MemoryLedger()
    ledger.add('ek', 'public', '2026-10-23', 6.99999)
    chat = client()
    with pytest.raises(BudgetStopped, match='Build-period'):
        call(Budget(ledger, now=NOW), chat)
    assert not chat.requests


@pytest.mark.parametrize('lane', ['public', 'judge'])
def test_november_daily_cap_counts_both_lanes(lane):
    ledger = MemoryLedger()
    ledger.add('ek', 'judge', '2026-11-05', 0.14999)
    now = lambda: datetime(2026, 11, 5, tzinfo=JST)
    chat = client()
    with pytest.raises(BudgetStopped, match='Daily ek'):
        call(Budget(ledger, now=now), chat, lane)
    assert not chat.requests


@pytest.mark.parametrize('lane,cap', [('public', 0.20), ('judge', 0.60)])
def test_december_caps_are_per_lane(lane, cap):
    now = lambda: datetime(2026, 12, 1, tzinfo=JST)
    ledger = MemoryLedger()
    ledger.add('ek', lane, '2026-12-01', cap-0.00001)
    chat = client()
    with pytest.raises(BudgetStopped, match='Daily '+lane):
        call(Budget(ledger, now=now), chat, lane)
    assert not chat.requests
    # The other lane has its independent daily allowance.
    call(Budget(ledger, now=now), client(), 'judge' if lane == 'public' else 'public')


def test_global_cap_counts_pw_and_all_dates():
    ledger = MemoryLedger()
    ledger.add('pw', 'public', '2025-01-01', 44)
    ledger.add('ek', 'judge', '2026-10-23', 0.99999)
    chat = client()
    with pytest.raises(BudgetStopped, match='Global'):
        call(Budget(ledger, now=NOW), chat)
    assert not chat.requests


def test_file_ledger_persists_and_reads_across_instances(tmp_path):
    path = tmp_path/'ledger.jsonl'
    first, second = FileLedger(path), FileLedger(path)
    first.add('ek', 'public', DAY, 0.05)
    second.add('pw', 'judge', DAY, 0.10)
    third = FileLedger(path)
    assert first.total() == second.total() == third.total() == Decimal('0.15')
    assert third.spent('ek', 'public', DAY) == Decimal('0.05')
    assert third.spent_period('ek', '2026-10-23', '2026-10-30') == Decimal('0.05')
    assert len(path.read_text().splitlines()) == 2


def test_file_lock_covers_admission_and_accounting(tmp_path):
    path = tmp_path/'ledger.jsonl'
    FileLedger(path).add('pw', 'public', DAY, 44.9993)
    def run():
        budget = Budget(FileLedger(path), now=NOW)
        try:
            call(budget, client({'prompt_tokens': 1000, 'completion_tokens': 2000}), max_tokens=2000)
            return 'admitted'
        except BudgetStopped:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(results) == ['admitted', 'refused']
    assert FileLedger(path).total() <= Decimal('45')


def test_invalid_ledger_fails_closed(tmp_path):
    path = tmp_path/'ledger.jsonl'
    path.write_text('not JSON\n')
    chat = client()
    with pytest.raises(BudgetStopped, match='Ledger unavailable'):
        call(Budget(FileLedger(path), now=NOW), chat)
    assert not chat.requests
    assert path.read_text() == 'not JSON\n'


def test_jst_october_period_boundary():
    ledger = MemoryLedger()
    ledger.add('ek', 'public', '2026-10-23', 6.99999)
    before = datetime(2026, 10, 30, 23, 59, tzinfo=JST)
    after = datetime(2026, 10, 31, 0, 0, tzinfo=JST)
    with pytest.raises(BudgetStopped, match='Build-period'):
        call(Budget(ledger, now=lambda: before), client())
    call(Budget(ledger, now=lambda: after), client())
    assert ledger.spent('ek', 'public', '2026-10-31') > 0
    assert ledger.spent('ek', 'public', '2026-10-30') == 0


def test_clock_utc_is_converted_to_jst():
    ledger = MemoryLedger()
    utc = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    call(Budget(ledger, now=lambda: utc), client())
    assert ledger.entries[0]['day'] == '2026-10-31'


@pytest.mark.parametrize('date', ['2026-10-22', '2026-12-17', '2027-10-24'])
def test_no_calls_outside_permitted_periods(date):
    now = lambda: datetime.fromisoformat(date).replace(tzinfo=JST)
    chat = client()
    with pytest.raises(BudgetStopped, match='Outside permitted'):
        call(Budget(now=now), chat)
    assert not chat.requests


def test_failed_call_is_accounted_conservatively():
    budget = Budget(now=NOW)
    chat = MockChatClient([{'error': 'failed', 'status': 500}])
    with pytest.raises(LLMError):
        call(budget, chat)
    assert budget.report()['total_usd'] > 0
    assert budget.report()['usage'] == 'estimated'


def test_retry_attempts_reserved_and_accounted():
    class RetryingClient:
        max_retries = 2
        def complete(self, *args, **kwargs):
            return ChatResult('{}', usage={'prompt_tokens': 10, 'completion_tokens': 10}, attempts=3)
    budget = Budget(now=NOW, run_cap=0.00005)
    with pytest.raises(BudgetStopped):
        call(budget, RetryingClient())  # Would fit once, but not three attempts.
    budget = Budget(now=NOW)
    call(budget, RetryingClient())
    assert budget.report()['stages']['test']['calls'] == 3
    assert budget.report()['usage'] == 'estimated'


def test_tool_and_schema_characters_count_toward_reservation():
    chat = client()
    budget = Budget(now=NOW, run_cap=0.0001)
    with pytest.raises(BudgetStopped):
        budget.complete(chat, 'test', 'ek', 'public', NANO, [], max_tokens=10,
                        tools=[{'description': 'x'*10_000}])
    assert not chat.requests


def test_naive_clock_refused():
    with pytest.raises(BudgetStopped, match='timezone aware'):
        call(Budget(now=lambda: datetime(2026, 10, 24)), client())


def test_run_cap_cannot_be_raised_above_owner_limit():
    budget = Budget(now=NOW, run_cap=100)
    assert budget.run_cap == Decimal('0.15')


@pytest.mark.parametrize('amount', [-1, float('inf'), float('nan')])
def test_ledger_rejects_negative_or_nonfinite_amounts(tmp_path, amount):
    for ledger in (MemoryLedger(), FileLedger(tmp_path/'ledger.jsonl')):
        with pytest.raises(ValueError):
            ledger.add('ek', 'public', DAY, amount)
