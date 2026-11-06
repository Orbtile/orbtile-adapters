#!/usr/bin/env python3
"""Toy agent: session, streaming text, a question; own terminal remains fallback."""
import secrets
import time
from orbtile_adapter import Client, AdapterError

registration = {'name': 'Tiny Python', 'palette': {'baseHue': 150, 'attentionHue': 330},
                'capabilities': ['tap', 'approvals', 'questions']}
with Client('dev.example.tiny.python', registration) as pad:
    pad.upsert_session('run', title='Tiny Python agent', activity='thinking', project='Demo jobs')
    for text in ('Hello', 'Hello from a tiny Python agent.'):
        pad.upsert_session('run', activity='streaming', message={'id': 'm1', 'text': text})
        time.sleep(0.2)
    question = {'id': 'q-' + secrets.token_hex(8), 'questions': [
        {'text': 'Which docs should I read?', 'header': 'Docs',
         'options': [{'label': 'README'}, {'label': 'Protocol'}]}]}
    pad.ask_question('run', question)
    answer = []
    def commit(event):
        if event['type'] == 'question':
            answer.append(event['picks'][0][0])
    print('Answer on the pad; terminal fallback in 30 seconds.')
    deadline = time.monotonic() + 30
    try:
        while not answer and time.monotonic() < deadline:
            for event in pad.poll_events()['events']:
                pad.apply(event, commit)
            time.sleep(0.1)  # mocks/disabled queues may return immediately
        if answer:
            pad.poll_events(0)  # acknowledge the committed answer, never lastSequence
    except (AdapterError, OSError):
        print('Pad unavailable; the terminal question is still authoritative.')
    if not answer:
        choice = input('README [0] or Protocol [1]: ').strip()
        if choice not in ('0', '1'):
            raise ValueError('Choose 0 or 1; no implicit answer')
        if pad.reconciliation_required:
            pad.reconcile()
            question['id'] = 'q-' + secrets.token_hex(8)
            pad.ask_question('run', question)
        pad.answer_locally('run', lambda: answer.append(int(choice)), request_id=question['id'])
    pad.upsert_session('run', activity='done', question=None,
                       message={'id': 'm2', 'text': 'You chose ' + question['questions'][0]['options'][answer[0]]['label']})
    print('Answer:', answer[0])
