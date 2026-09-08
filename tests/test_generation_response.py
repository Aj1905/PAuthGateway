from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from gateway.planning.generation_response import GenerationFailure, GenerationSession, call_generator


@pytest.fixture(autouse=True)
def _p4_prompt_path(monkeypatch):
    """These tests drive the generator with fixed fake outputs that carry no
    inventory block. Since p5 (2026-09-08) the self-repair loop reconciles an
    action inventory by default, which would add repair rounds and change the
    call counts asserted here; pin the p4 prompt path (claude, coordinated on
    the board)."""
    monkeypatch.setenv("PAUTH_PLANNER_INVENTORY", "0")


def anthropic(reason, text=''):
    return NS(messages=NS(create=Mock(return_value=NS(stop_reason=reason, content=[NS(type='text', text=text)], usage=NS(input_tokens=10, output_tokens=2)))))


def openai(reason='stop', text='def run():\n    pass', refusal=None):
    return NS(chat=NS(completions=NS(create=Mock(return_value=NS(choices=[NS(finish_reason=reason, message=NS(content=text, refusal=refusal))], usage=NS(prompt_tokens=20, completion_tokens=3))))))


@pytest.mark.parametrize('reason,text,expected', [('refusal', '', 'refusal'), ('max_tokens', 'partial', 'max_tokens'), ('end_turn', '', 'empty_response')])
def test_anthropic_failures(reason, text, expected):
    with pytest.raises(GenerationFailure) as raised:
        call_generator(anthropic(reason, text), 'claude-test', [])
    assert raised.value.reason == expected
    assert raised.value.prompt_tokens == 10


@pytest.mark.parametrize('reason,text,refusal,expected', [('stop', '', 'blocked', 'refusal'), ('content_filter', '', None, 'refusal'), ('length', 'partial', None, 'length'), ('stop', '', None, 'empty_response')])
def test_openai_failures(reason, text, refusal, expected):
    with pytest.raises(GenerationFailure) as raised:
        call_generator(openai(reason, text, refusal), 'gpt-test', [])
    assert raised.value.reason == expected


def test_fallback_records_usage_and_stays_on_second_model(caplog):
    resolver = Mock(return_value=openai())
    session = GenerationSession('claude-test', anthropic('refusal'), fallback_model='gpt-test', resolve_client=resolver)
    text, pt, ct = session([])
    assert text.startswith('def run') and (pt, ct) == (30, 5)
    assert session.calls == 2 and len(session.failure_history) == 1
    assert 'stop_reason=refusal' in caplog.text
    assert session.cost(lambda model, pt, ct: pt * (1 if model == 'claude-test' else 2)) == 50
    session([])
    assert resolver.call_count == 1 and session.calls == 3
    assert session.cost(lambda *args: None) is None


def test_both_refuse_without_cycle():
    session = GenerationSession('claude-test', anthropic('refusal'), fallback_model='gpt-test', resolve_client=lambda *_: openai(refusal='no'))
    with pytest.raises(GenerationFailure):
        session([])
    assert session.calls == 2 and len(session.failure_history) == 2


def test_empty_does_not_fallback():
    resolver = Mock()
    session = GenerationSession('claude-test', anthropic('end_turn'), fallback_model='gpt-test', resolve_client=resolver)
    with pytest.raises(GenerationFailure):
        session([])
    resolver.assert_not_called()


def test_same_model_rejected():
    with pytest.raises(ValueError):
        GenerationSession('a', None, fallback_model='a', resolve_client=Mock())


def test_integrated_fallback_cache_and_validation(tmp_path):
    import json
    from gateway.planning.agentic_planner import generate_code_with_self_repair
    from pauth.codegen import ToolDoc
    tools = [ToolDoc(name='get_items', description='list items', parameters=[], returns='list')]
    path = tmp_path / 'plan.py'
    result = generate_code_with_self_repair('list the items', tools, model='claude-test',
        client=anthropic('refusal'), fallback_model='gpt-4.1',
        fallback_client=openai(text='def run():\n    items = get_items()\n'),
        enable_judge=False, max_retries=0, cache_path=path)
    assert result.attempts == 2 and result.model == 'gpt-4.1'
    assert result.prompt_tokens == 30 and result.completion_tokens == 5
    assert result.failure_history == ['generation: model=claude-test stop_reason=refusal']
    assert json.loads(path.with_suffix('.json').read_text())['failure_history'] == result.failure_history
    cached = generate_code_with_self_repair('list the items', tools, cache_path=path)
    assert cached.cached and cached.code == result.code


@pytest.mark.parametrize('reason', ['refusal', 'end_turn', 'max_tokens'])
def test_integrated_failures_never_cache(tmp_path, reason):
    from gateway.planning.agentic_planner import generate_code_with_self_repair
    path = tmp_path / 'plan.py'
    with pytest.raises(GenerationFailure):
        generate_code_with_self_repair('list', [], model='claude-test',
            client=anthropic(reason), fallback_model='gpt-test', fallback_client=openai(refusal='no'),
            enable_judge=False, cache_path=path)
    assert not path.exists() and not path.with_suffix('.json').exists()


def test_empty_legacy_cache_regenerated(tmp_path):
    from gateway.planning.agentic_planner import generate_code_with_self_repair
    path = tmp_path / 'plan.py'
    path.write_text('  \n')
    client = openai()
    result = generate_code_with_self_repair('nothing', [], model='gpt-4.1', client=client,
        enable_judge=False, cache_path=path, max_retries=0)
    assert not result.cached and client.chat.completions.create.call_count == 1


def test_fallback_does_not_skip_grammar(tmp_path):
    from gateway.planning.agentic_planner import generate_code_with_self_repair
    result = generate_code_with_self_repair('anything', [], model='claude-test',
        client=anthropic('refusal'), fallback_model='gpt-4.1', fallback_client=openai(text='not python !'),
        enable_judge=False, max_retries=0)
    assert any(f.startswith('grammar:') for f in result.failure_history)


@pytest.mark.parametrize("text", ["```python\n```", "```python\n\n```", "```\n```"] )
def test_empty_fenced_plan_never_cached(tmp_path, text):
    from gateway.planning.agentic_planner import generate_code_with_self_repair
    path = tmp_path / 'plan.py'
    with pytest.raises(GenerationFailure, match='empty_plan'):
        generate_code_with_self_repair('nothing', [], model='gpt-4.1',
            client=openai(text=text), enable_judge=False, cache_path=path)
    assert not path.exists()
