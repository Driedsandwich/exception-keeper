"""Mock and loopback-only integration coverage; never calls a remote provider."""
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
import time

import pytest
from openpyxl import Workbook

from sheetaudit import audit
from sheetaudit.budget import Budget, JST
from sheetaudit.llm.client import ChatResult, HttpChatClient, LLMError, MockChatClient
from sheetaudit.llm.jsonout import decode, parse, request_json, response_format, validate
from sheetaudit.llm.models import NANO, SUPER, ULTRA
from sheetaudit.loader import load
from sheetaudit.review import review
from sheetaudit.review.judge import ground, judge
from sheetaudit.review.schemas import JUDGMENT, OUTLINE, obj, text
from sheetaudit.review.structure import compact_outline, validate_outline
from sheetaudit.review.tools import WorkbookTools

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'tests/fixtures/mock_scripts'
KEY = 'test-key-not-secret'
NOW = lambda: datetime(2026, 10, 24, 12, tzinfo=JST)


def script(name='club_membership_fees'):
    return json.loads((SCRIPTS / (name + '.json')).read_text())


def budget(**kwargs):
    return Budget(now=NOW, **kwargs)


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv('NEBIUS_API_KEY', KEY)
    servers = []

    def start(responses, check=None):
        pending, received, errors = list(deepcopy(responses)), [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.path == '/v1/chat/completions'
                    assert self.headers.get('Authorization') == 'Bearer ' + KEY
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    received.append(payload)
                    assert pending, 'Unexpected extra HTTP request'
                    item = pending.pop(0)
                    if item.get('model'):
                        assert payload['model'] == item['model']
                    if check:
                        check(payload)
                    status = item.get('_status', 200)
                    if status != 200:
                        self.send_response(status)
                        self.send_header('Retry-After', item.get('_retry_after', '0'))
                        self.end_headers()
                        self.wfile.write(KEY.encode())  # Provider error bodies must never escape.
                        return
                    message = dict(role='assistant', content=item.get('content'),
                                   reasoning_content='Internal reasoning must be ignored')
                    if item.get('tool_calls'):
                        message['tool_calls'] = item['tool_calls']
                    body = dict(choices=[{'message': message}], model=payload['model'])
                    if 'usage' in item:
                        body['usage'] = item['usage']
                    data = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    for key, value in item.get('rate_headers', {}).items():
                        self.send_header(key, value)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': ' + str(exc))
                    self.send_response(500)
                    self.end_headers()

        server = HTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
        try:
            server.server_bind()
        except OSError as exc:
            server.server_close()
            pytest.skip('Loopback bind failed: ' + str(exc))
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        servers.append((server, thread, errors))
        return HttpChatClient(f'http://127.0.0.1:{server.server_port}/v1/', timeout_s=2), received, pending

    yield start
    for server, thread, errors in servers:
        server.shutdown()
        thread.join(timeout=1)
        server.server_close()
        assert not errors, errors


@pytest.mark.parametrize('style', ['nebius_doc', 'openai', 'json_object', 'none'])
def test_http_schema_styles(stub, style):
    schema = obj({'answer': text()})
    def check(payload):
        if style == 'none':
            assert 'response_format' not in payload
        else:
            assert payload['response_format'] == response_format(schema, style)
        assert payload['model'] == NANO
    client, requests, pending = stub([dict(content='{"answer":"ok"}', model=NANO,
                                          usage={'prompt_tokens': 10, 'completion_tokens': 5},
                                          rate_headers={'x-ratelimit-remaining-requests': '9',
                                                        'x-ratelimit-remaining-tokens': '500',
                                                        'x-ratelimit-over-limit': 'yes'})], check)
    response = client.complete(NANO, [{'role': 'user', 'content': 'JSON please'}],
                               response_format=response_format(schema, style))
    assert parse(response.content) == {'answer': 'ok'}
    assert response.usage['completion_tokens'] == 5
    assert response.rate_headers['x-ratelimit-over-limit'] == 'yes'
    assert not pending
    assert KEY not in repr(client) + repr(response) + json.dumps(requests)


def test_retry_429_then_success(stub, monkeypatch):
    waits = []
    monkeypatch.setattr('sheetaudit.llm.client.time.sleep', waits.append)
    client, requests, pending = stub([{'_status': 429, '_retry_after': '100'}, {'content': '{}'}])
    client.timeout_s = 10  # Leave room for the capped 2 s wait inside the total time budget.
    response = client.complete(NANO, [])
    assert response.attempts == 2
    assert waits == [2.0]
    assert len(requests) == 2


@pytest.mark.parametrize('retry_after', ['Wed, 01 Jan 2099 00:00:00 GMT', 'invalid'])
def test_retry_after_date_or_malformed(stub, monkeypatch, retry_after):
    waits = []
    monkeypatch.setattr('sheetaudit.llm.client.time.sleep', waits.append)
    client, _, _ = stub([{'_status': 429, '_retry_after': retry_after}, {'content': '{}'}])
    client.timeout_s = 10  # Leave room for the capped 2 s wait inside the total time budget.
    client.complete(NANO, [])
    assert len(waits) == 1 and 0 <= waits[0] <= 2


def test_500_twice_then_error(stub, caplog):
    client, requests, _ = stub([{'_status': 500}, {'_status': 500}])
    client.max_retries = 1
    with pytest.raises(LLMError) as failure:
        client.complete(SUPER, [])
    assert failure.value.status == 500
    assert len(requests) == 2
    assert KEY not in str(failure.value) + repr(failure.value) + repr(client) + caplog.text


@pytest.mark.parametrize('status', [400, 401, 403, 404])
def test_other_4xx_never_retry(stub, status):
    client, received, _ = stub([{'_status': status}])
    with pytest.raises(LLMError):
        client.complete(NANO, [])
    assert len(received) == 1


def test_total_time_budget_refuses_retry_wait(stub):
    client, received, _ = stub([{'_status': 429, '_retry_after': '2'}])
    client.timeout_s = 0.2
    with pytest.raises(LLMError, match='time budget'):
        client.complete(NANO, [])
    assert len(received) == 1


def test_key_is_read_at_call_time_and_not_in_repr(monkeypatch):
    client = HttpChatClient()
    monkeypatch.delenv('NEBIUS_API_KEY', raising=False)
    with pytest.raises(LLMError, match='Missing API key') as exc:
        client.complete(NANO, [])
    assert KEY not in repr(client) + repr(exc.value)


def test_secret_echo_is_redacted(stub, caplog):
    client, _, _ = stub([dict(content=KEY, rate_headers={'x-ratelimit-over-limit': KEY})])
    result = client.complete(NANO, [])
    assert KEY not in repr(result) + caplog.text


def test_reasoning_fences_and_balanced_prose():
    content = '<think>{"wrong":true}</think>\n```json\n{"text":"brace } and \\\" quote", "nested":{"a":1}}\n```'
    assert parse(content)['nested'] == {'a': 1}
    assert parse('prose { invalid } then {"ok": [1, {"a":2}]} trailing')['ok'][1]['a'] == 2
    with pytest.raises(ValueError):
        parse('<think>{"wrong":true}')


@pytest.mark.parametrize('bad', ['not JSON', '{"answer":3}', '{"answer":"ok","extra":1}'])
def test_http_one_repair_retry(stub, bad):
    schema = obj({'answer': text()})
    client, requests, _ = stub([dict(content=bad), dict(content='<think>hidden</think>```json\n{"answer":"ok"}\n```')])
    value, errors = request_json(client.complete, NANO, [], schema)
    assert value == {'answer': 'ok'} and not errors
    assert 'Validation errors' in requests[1]['messages'][-1]['content']
    assert len(requests) == 2


def test_twice_invalid_is_structured_failure():
    client = MockChatClient([{'content': 'bad'}, {'content': 'still bad'}])
    value, errors = request_json(client.complete, NANO, [], obj({'answer': text()}))
    assert value is None and errors and len(client.requests) == 2


def test_validator_types_required_enum_arrays_lengths_extra():
    schema = obj({'items': {'type': 'array', 'items': {'type': 'integer'}},
                  'kind': {'type': 'string', 'enum': ['a'], 'maxLength': 1}})
    assert validate({'items': [True], 'kind': 'too long', 'extra': 2}, schema)
    assert validate({}, schema)
    assert not validate({'items': [1], 'kind': 'a'}, schema)
    assert decode('{"items":[NaN],"kind":"a"}', schema)[0] is None


def test_env_schema_style(monkeypatch):
    monkeypatch.setenv('EK_SCHEMA_STYLE', 'nebius_doc')
    schema = obj({})
    assert response_format(schema) == {'type': 'json_schema', 'json_schema': schema}
    with pytest.raises(ValueError):
        response_format(schema, 'unknown')


@pytest.mark.parametrize('sample', ['club_membership_fees', 'small_business_budget', 'event_budget'])
def test_full_mock_ground_truth(sample, caplog, monkeypatch):
    monkeypatch.setenv('NEBIUS_API_KEY', KEY)
    fixture = script(sample)
    client = MockChatClient(fixture)
    report = review(ROOT / 'samples' / (sample + '.xlsx'), client, budget())
    assert report['source'] == 'mock' and not report['stopped_reason']
    findings = {f['sheet']+'!'+f['cell']: f for f in report['findings']}
    for expected in fixture['expectations']:
        actual = findings[expected['cell']]
        assert actual['verdict'] == expected['final_verdict']
        assert actual['grounding'] == expected['grounding']
        assert actual['impact'] == expected['impact']
    assert all(f['decided_by'] == SUPER for f in findings.values())
    assert KEY not in json.dumps(report) + repr(client) + repr(client.requests) + caplog.text


def test_full_http_pipeline_tool_roundtrip_and_downgrades(stub, caplog):
    fixture = script('grounding_failures')
    fixture['responses'][0]['content'] = '<think>Ignore this reasoning</think>```json\n' + fixture['responses'][0]['content'] + '\n```'
    client, received, pending = stub(fixture['responses'])
    report = review(ROOT/'samples/club_membership_fees.xlsx', client, budget())
    assert not pending and report['stopped_reason'] is None
    findings = {f['sheet']+'!'+f['cell']: f for f in report['findings']}
    for expected in fixture['expectations']:
        actual = findings[expected['cell']]
        assert actual['verdict'] == expected['final_verdict']
        assert actual['grounding'] == expected['grounding']
        assert actual['impact'] == expected['impact']
    assert findings['Members!C6']['original_verdict'] == 'error'
    assert findings['Members!C26']['original_verdict'] == 'intended_exception'
    assert findings['Members!C32']['impact'] == 50
    tool_messages = [m for r in received for m in r['messages'] if m['role'] == 'tool']
    assert any('Waived' in m['content'] and 'Notes' in m['content'] for m in tool_messages)
    assert report['cost']['usage'] == 'reported'
    assert report['cost']['rate_headers'][0]['headers']['x-ratelimit-remaining-requests'] == '99'
    assert KEY not in json.dumps(report) + repr(client) + caplog.text


@pytest.fixture
def context():
    path = ROOT/'samples/club_membership_fees.xlsx'
    book = load(path)
    candidate = audit(path)[0]
    outline = json.loads(script()['responses'][0]['content'])
    return book, candidate, outline


@pytest.mark.parametrize('name,args', [
    ('read_range', {'sheet': 'Members', 'range': 'A1:D20'}),
    ('read_range', {'sheet': 'Missing', 'range': 'A1'}),
    ('read_range', {'sheet': 'Members', 'range': 'A1', 'extra': 'bad'}),
    ('read_range', {'sheet': 'Members', 'range': 'A0'}),
    ('read_range', {'sheet': 'Members', 'range': 'A1:ZZ99999999999999999999999999'}),
    ('read_range', []), ('read_range', 'not JSON'),
    ('find_notes', {'sheet': 'Members', 'row': True}),
    ('find_notes', {'sheet': 'Members', 'row': 99}),
    ('find_rule', {'keyword': ''}), ('unknown_tool', {}),
    ('same_pattern_cells', {'sheet': 'Members', 'cell': 'A1:B2'}),
])
def test_bad_tool_arguments_are_errors(context, name, args):
    book, candidate, outline = context
    assert 'error' in WorkbookTools(book, outline, candidate).execute(name, args)


def test_tools_bounds_and_notes(context):
    book, candidate, outline = context
    tools = WorkbookTools(book, outline, candidate)
    assert len(tools.read_range('Members', 'A1:D15')) == 60
    notes = tools.find_notes('Members', 20)
    assert notes['text'] == 'Waived — board decision' and notes['header']['value'] == 'Notes'
    assert tools.find_notes('Fee Rules', 3)['text'] is None
    assert tools.find_rule('rate') and len(tools.find_rule('e')) <= 10
    patterns = tools.same_pattern_cells('Members', 'C14')
    assert patterns and len(patterns) <= 20 and all('formula' in c for c in patterns)


def test_four_round_limit_and_bad_arguments_returned(context):
    book, candidate, outline = context
    tool_response = {'tool_calls': [{'id': 'call', 'type': 'function', 'function': {
        'name': 'read_range', 'arguments': '{"sheet":"Missing","range":"A1"}'}}]}
    client = MockChatClient([tool_response]*4)
    result = judge(candidate, book, outline, client.complete)
    assert result['verdict'] == 'ask' and '4' in result['reason']
    assert len(client.requests) == 4
    assert 'error' in client.requests[1]['messages'][-1]['content']


def test_json_only_upfront_lookups(context):
    book, candidate, outline = context
    response = script()['responses'][1]
    client = MockChatClient([response])
    result = judge(candidate, book, outline, client.complete, judge_mode='json_only')
    assert result['verdict'] == 'error'
    req = client.requests[0]
    assert not req['tools']
    lookups = json.loads(req['messages'][1]['content'])['lookups']
    assert set(lookups) == {'read_range', 'find_notes', 'find_rule', 'same_pattern_cells'}


@pytest.mark.parametrize('mutation', ['unknown_cell', 'empty_quote', 'id_mismatch', 'fabricated', 'missing_note'])
def test_grounding_failures(context, mutation):
    book, candidate, outline = context
    answer = json.loads(script()['responses'][1]['content'])
    if mutation == 'unknown_cell': answer['evidence'][0]['cell'] = 'Missing!C6'
    if mutation == 'empty_quote': answer['evidence'][0]['quote'] = ''
    if mutation == 'id_mismatch': answer['candidate_id'] = 'wrong'
    if mutation == 'fabricated': answer['evidence'][0]['quote'] = 'does not exist'
    if mutation == 'missing_note': answer['verdict'] = 'intended_exception'
    result = ground(answer, candidate, book, outline)
    assert result['verdict'] == 'ask' and result['grounding'] == 'failed'
    assert result['original_verdict'] == answer['verdict']


def test_case_whitespace_grounding_and_rule_quote(context):
    book, candidate, outline = context
    answer = json.loads(script()['responses'][1]['content'])
    answer['evidence'] = [{'cell': "'Fee Rules'!A3", 'quote': 'sTudent   RATE'}]
    answer['verdict'] = 'intended_exception'
    result = ground(answer, candidate, book, outline)
    assert result['grounding'] == 'passed' and result['verdict'] == 'intended_exception'


@pytest.mark.parametrize('formula,kept', [('=B6', True), ("='Fee Rules'!B2", True),
    ('B6', False), ('=Missing!B2', False), ('=SUM(Missing!B2:B3)', False),
    ('=SUM(Members:Missing!B2)', False), ('=INDIRECT("Missing!B2")', False),
    ('=[external.xlsx]Data!A1', False), ('=', False)])
def test_fixes_checked(context, formula, kept):
    book, candidate, outline = context
    answer = json.loads(script()['responses'][1]['content'])
    answer['fix'] = {'cell': 'Members!C6', 'new_formula': formula}
    assert (ground(answer, candidate, book, outline)['fix'] is not None) == kept


def test_outline_drops_invalid_fields(context):
    book, _, outline = context
    outline['sheets'][0].update(header_row=999, label_column='ZZ', notes_column='?',
                                total_rows=[32, 999], rules_ranges=['A1:B2', 'A1:Z999'])
    outline['sheets'].append(dict(outline['sheets'][0], name='Missing'))
    warnings = []
    validated = validate_outline(outline, book, warnings)
    entry = validated['sheets'][0]
    assert not {'header_row', 'label_column', 'notes_column'} & entry.keys()
    assert entry['total_rows'] == [32] and entry['rules_ranges'] == ['A1:B2']
    assert len(validated['sheets']) == 2 and warnings


def test_privacy_sentinel_far_away(tmp_path):
    path = tmp_path/'private.xlsx'
    wb = Workbook()
    tab = wb.active
    tab.title = 'Data'
    tab.append(['Label', 'Value', 'Notes'])
    for row in range(2, 7):
        tab.append([f'Item {row}', f'=A{row}' if row != 4 else 17])
    sentinel = 'PRIVATE_SENTINEL_FAR_AWAY_92817'
    tab['Z200'] = sentinel
    wb.save(path)
    outline = {'sheets': [{'name': 'Data', 'role': 'data', 'header_row': 1,
               'label_column': 'A', 'notes_column': 'C', 'total_rows': [], 'rules_ranges': []}]}
    candidate = next(c for c in audit(path) if c.cell == 'B4')
    answer = dict(candidate_id=candidate.id, verdict='ask', confidence='low', reason='Confirm intent',
                  evidence=[{'cell': 'Data!B4', 'quote': '17'}], fix=None, question='Intentional?')
    summary = dict(headline='Confirm intent', order=[candidate.id], conflicts=[], question_for_user=None)
    client = MockChatClient([{'content': json.dumps(v)} for v in (outline, answer, summary)])
    report = review(path, client, budget(), max_candidates=1, judge_mode='json_only')
    assert sentinel not in json.dumps(client.requests)
    assert report['findings'][0]['decided_by'] == SUPER
    assert all('Do not compute numbers' in r['messages'][0]['content'] for r in client.requests)
    assert len(compact_outline(load(path))) <= 6000


def test_limits_and_model_failures_are_explicit():
    fixture = script('event_budget')
    # No call is needed when no candidate is selected.
    client = MockChatClient([])
    report = review(ROOT/'samples/event_budget.xlsx', client, budget(), max_candidates=0)
    assert all(f['verdict'] == 'not reviewed (limit)' for f in report['findings'])
    assert not client.requests
    client = MockChatClient([{'content': 'bad'}]*20)
    report = review(ROOT/'samples/event_budget.xlsx', client, budget())
    assert all(f['decided_by'] == 'fallback' for f in report['findings'])
    assert report['warnings']
    assert len(client.requests) == 8  # Each of structure, 2 candidates, summary repairs once.


def test_summary_unknown_ids_dropped():
    fixture = script('event_budget')
    summary = json.loads(fixture['responses'][-1]['content'])
    summary['order'].append('unknown')
    summary['conflicts'] = [{'candidate_ids': summary['order'], 'explanation': 'example'}]
    fixture['responses'][-1]['content'] = json.dumps(summary)
    report = review(ROOT/'samples/event_budget.xlsx', MockChatClient(fixture), budget())
    assert 'unknown' not in json.dumps(report['summary'])
    assert len(report['summary']['conflicts'][0]['candidate_ids']) == 2
    assert report['warnings']

@pytest.fixture
def inprocess_http(monkeypatch):
    """Exercise urllib request/response handling without a socket or remote service."""
    from io import BytesIO
    from urllib.error import HTTPError
    monkeypatch.setenv('NEBIUS_API_KEY', KEY)
    received = []
    pending = []

    class Response(BytesIO):
        headers = {'x-ratelimit-remaining-requests': '8'}

    class Opener:
        def open(self, req, timeout):
            assert req.full_url == 'http://127.0.0.1:1/v1/chat/completions'
            assert req.get_header('Authorization') == 'Bearer ' + KEY
            payload = json.loads(req.data)
            received.append(payload)
            assert pending, 'Unexpected extra request'
            item = pending.pop(0)
            if item.get('model'):
                assert payload['model'] == item['model']
            if item.get('_status'):
                raise HTTPError(req.full_url, item['_status'], KEY, {'Retry-After': '0'}, BytesIO(KEY.encode()))
            if '_raw' in item:
                return Response(item['_raw'])
            message = dict(content=item.get('content'), reasoning_content=KEY)
            if item.get('tool_calls'):
                message['tool_calls'] = item['tool_calls']
            value = dict(choices=[{'message': message}], usage=item.get('usage'), model=payload['model'])
            return Response(json.dumps(value).encode())

    monkeypatch.setattr('sheetaudit.llm.client.request.build_opener', lambda *args: Opener())
    return HttpChatClient('http://127.0.0.1:1/v1/', timeout_s=1), received, pending


def test_http_pipeline_without_socket_including_downgrades(inprocess_http, caplog):
    client, received, pending = inprocess_http
    fixture = script('grounding_failures')
    pending.extend(fixture['responses'])
    report = review(ROOT/'samples/club_membership_fees.xlsx', client, budget())
    assert not pending
    for finding, expected in zip(report['findings'], fixture['expectations'], strict=True):
        assert finding['verdict'] == expected['final_verdict']
        assert finding['grounding'] == expected['grounding']
        assert finding['impact'] == expected['impact']
    assert any(m['role'] == 'tool' for r in received for m in r['messages'])
    assert KEY not in repr(report) + repr(client) + caplog.text


@pytest.mark.parametrize('style', ['nebius_doc', 'openai', 'json_object', 'none'])
def test_http_format_encoding_without_socket(inprocess_http, style):
    client, received, pending = inprocess_http
    pending.append(dict(content='<think>hidden</think>```json\n{"ok":"yes"}\n```'))
    schema = obj({'ok': text()})
    result, errors = request_json(client.complete, NANO, [], schema, schema_style=style)
    assert result == {'ok': 'yes'} and not errors
    assert received[0].get('response_format') == response_format(schema, style)


def test_http_retry_and_one_repair_without_socket(inprocess_http):
    client, received, pending = inprocess_http
    pending.extend([{'_status': 429}, {'content': 'bad JSON'}, {'content': '{"ok":"yes"}'}])
    value, errors = request_json(client.complete, NANO, [], obj({'ok': text()}))
    assert value == {'ok': 'yes'} and not errors
    assert len(received) == 3
    assert 'Validation errors' in received[-1]['messages'][-1]['content']


@pytest.mark.parametrize('status,attempts', [(500, 3), (429, 3), (401, 1)])
def test_http_errors_without_socket(inprocess_http, status, attempts):
    client, received, pending = inprocess_http
    pending.extend([{'_status': status}] * attempts)
    with pytest.raises(LLMError) as error:
        client.complete(SUPER, [])
    assert len(received) == attempts
    assert KEY not in str(error.value) + repr(error.value) + repr(client)


@pytest.mark.parametrize('raw', [b'not JSON', b'{"choices": []}', b'{"choices":[{"message":null}]}',
                               b'{"choices":[{"message":{"content":[],"tool_calls":{}}}]}'])
def test_malformed_http_response_without_socket(inprocess_http, raw):
    client, received, pending = inprocess_http
    pending.append({'_raw': raw})
    with pytest.raises(LLMError):
        client.complete(NANO, [])
    assert len(received) == 1


def test_context_quote_cited_at_unrelated_cell_is_ungrounded():
    from sheetaudit.loader import load
    from sheetaudit.review.judge import ground
    path = ROOT/'samples/club_membership_fees.xlsx'
    book = load(path)
    candidate = next(c for c in audit(path) if c.cell == 'C20')
    note_word = candidate.context['notes'].split()[0]
    outline = {'sheets': [{'name': name, 'role': 'data'} for name in book.sheets]}
    value = dict(candidate_id=candidate.id, verdict='error', confidence='high', reason='r',
                 evidence=[{'cell': 'Members!B2', 'quote': note_word}], fix=None, question=None)
    result = ground(value, candidate, book, outline)
    assert result['verdict'] == 'ask' and result['grounding'] == 'failed'
    value['evidence'] = [{'cell': 'Members!' + candidate.context['notes_cell'], 'quote': note_word}]
    assert ground(value, candidate, book, outline)['grounding'] == 'passed'


def test_bare_evidence_address_means_candidate_sheet():
    # Live Nemotron Super answers cited "E5" instead of "Event!E5" (2026-10-24).
    from sheetaudit.loader import load
    path = ROOT/'samples/event_budget.xlsx'
    book = load(path)
    candidate = next(c for c in audit(path) if c.cell == 'D5')
    outline = {'sheets': [{'name': name, 'role': 'data'} for name in book.sheets]}
    value = dict(candidate_id=candidate.id, verdict='intended_exception', confidence='high', reason='r',
                 evidence=[{'cell': 'E5', 'quote': 'Manual override: venue quote received'}],
                 fix={'cell': 'D5', 'new_formula': '=B5*C5'}, question=None)
    result = ground(value, candidate, book, outline)
    assert result['grounding'] == 'passed' and result['verdict'] == 'intended_exception'
    assert result['evidence'][0]['cell'] == 'Event!E5' and result['fix']['cell'] == 'Event!D5'
    value['evidence'] = [{'cell': 'E4', 'quote': 'Manual override: venue quote received'}]
    result = ground(value, candidate, book, outline)
    assert result['grounding'] == 'failed' and result['verdict'] == 'ask' and result['fix'] is None


def test_empty_quote_only_proves_an_empty_cell():
    from sheetaudit.loader import load
    path = ROOT/'samples/event_budget.xlsx'
    book = load(path)
    candidate = next(c for c in audit(path) if c.cell == 'D4')
    outline = {'sheets': [{'name': name, 'role': 'data'} for name in book.sheets]}
    value = dict(candidate_id=candidate.id, verdict='error', confidence='high', reason='r',
                 evidence=[{'cell': 'C4', 'quote': ''}], fix=None, question=None)
    assert ground(value, candidate, book, outline)['grounding'] == 'passed'
    # An empty quote never documents an exception, and fails at a cell that has content.
    assert ground(dict(value, verdict='intended_exception'), candidate, book, outline)['grounding'] == 'failed'
    for cell in ('B4', 'D4', 'Event!B4'):
        value['evidence'] = [{'cell': cell, 'quote': ''}]
        assert ground(value, candidate, book, outline)['grounding'] == 'failed', cell
