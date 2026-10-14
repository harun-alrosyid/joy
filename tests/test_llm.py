"""Exercise real OpenAI Responses and Chat Completions serialization without network access."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
from openai import OpenAI

from joy.config import CHAT, DEFAULT_MODEL, RESPONSES, Settings
from joy.llm import ToolEvent, stream_reply


def sse(events):
    return httpx.Response(200, text=''.join(
        f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events
    ), headers={'content-type': 'text/event-stream'})


def chunk(delta, finish_reason=None):
    return {'type': 'chat.completion.chunk', 'id': 'chatcmpl', 'object': 'chat.completion.chunk',
            'created': 0, 'model': DEFAULT_MODEL,
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]}


def stream_chunks(chunks):
    return httpx.Response(200, text=''.join(
        f'data: {json.dumps(item)}\n\n' for item in chunks
    ) + 'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})


def completed(output=(), status='completed'):
    return {'type': 'response.' + status, 'response': {
        'id': 'resp_test', 'object': 'response', 'created_at': 0,
        'status': status, 'output': list(output),
    }}


async def collect_reply(messages, settings):
    return ''.join([part async for part in stream_reply(messages, settings) if isinstance(part, str)])


class LlmTest(unittest.IsolatedAsyncioTestCase):
    def settings(self, api=RESPONSES, **overrides):
        values = {'api': api, 'model': DEFAULT_MODEL, 'api_key': 'test-key',
                  'base_url': 'https://api.openai.com/v1'}
        values.update(overrides)
        return Settings(**values)

    def client_patch(self, respond):
        transport = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(transport.close)

        def factory(**config):
            config.setdefault('api_key', 'test-key')
            config.setdefault('base_url', 'https://api.openai.com/v1')
            return OpenAI(**config, http_client=transport)

        return patch('joy.llm.OpenAI', side_effect=factory)

    async def test_reasoning_survives_tool_call_and_continuation(self):
        payloads = []
        reasoning = {'type': 'reasoning', 'id': 'rs_1', 'summary': [],
                     'encrypted_content': 'opaque-reasoning'}
        call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                'name': 'write', 'arguments': json.dumps({'path': 'hello.txt', 'content': 'hello'}),
                'status': 'completed'}

        def respond(request):
            self.assertEqual(request.url.path, '/v1/responses')
            payload = json.loads(request.content)
            payloads.append(payload)
            self.assertEqual(payload['reasoning'], {'effort': 'medium'})
            self.assertFalse(payload['store'])
            self.assertEqual(payload['include'], ['reasoning.encrypted_content'])
            self.assertNotIn('reasoning_effort', payload)
            self.assertEqual([tool['name'] for tool in payload['tools']], ['read', 'write', 'edit', 'bash'])
            self.assertTrue(all(tool['strict'] is False for tool in payload['tools']))
            if len(payloads) == 1:
                return sse([
                    {'type': 'response.function_call_arguments.delta', 'delta': '{"path":'},
                    {'type': 'response.function_call_arguments.delta', 'delta': '"hello.txt"}'},
                    completed([reasoning, call]),
                ])
            self.assertEqual(payload['input'][-3:-1], [reasoning, call])
            self.assertEqual(payload['input'][-1]['type'], 'function_call_output')
            self.assertEqual(payload['input'][-1]['call_id'], 'call_1')
            self.assertIn('Wrote hello.txt', payload['input'][-1]['output'])
            return sse([{'type': 'response.output_text.delta', 'delta': 'Created it.'}, completed()])

        with tempfile.TemporaryDirectory() as directory:
            with patch('joy.llm.Path.cwd', return_value=Path(directory)), self.client_patch(respond):
                chunks = [part async for part in stream_reply(
                    [{'role': 'user', 'content': 'Create it'}], self.settings())]
            self.assertEqual(chunks, [ToolEvent('write', 'running'), ToolEvent('write', 'finished'), 'Created it.'])
            self.assertEqual((Path(directory) / 'hello.txt').read_text(), 'hello')
        self.assertEqual(len(payloads), 2)

    async def test_chat_tool_call_and_continuation(self):
        payloads = []

        def respond(request):
            self.assertEqual(request.url.path, '/v1/chat/completions')
            payload = json.loads(request.content)
            payloads.append(payload)
            self.assertTrue(payload['stream'])
            self.assertFalse(payload['parallel_tool_calls'])
            self.assertEqual([tool['function']['name'] for tool in payload['tools']],
                             ['read', 'write', 'edit', 'bash'])
            if len(payloads) == 1:
                return stream_chunks([
                    chunk({'role': 'assistant', 'content': 'Working'}),
                    chunk({'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function',
                                           'function': {'name': 'write', 'arguments': ''}}]}),
                    chunk({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '{"path":"hello.txt",'}}]}),
                    chunk({'tool_calls': [{'index': 0,
                                           'function': {'arguments': '"content":"hello"}'}}]}),
                    chunk({}, finish_reason='tool_calls'),
                ])
            self.assertEqual(payload['messages'][-2]['role'], 'assistant')
            self.assertEqual(payload['messages'][-2]['tool_calls'][0]['function']['name'], 'write')
            self.assertEqual(payload['messages'][-1]['role'], 'tool')
            self.assertEqual(payload['messages'][-1]['tool_call_id'], 'call_1')
            self.assertIn('Wrote hello.txt', payload['messages'][-1]['content'])
            return stream_chunks([
                chunk({'role': 'assistant', 'content': 'Created'}),
                chunk({'content': ' it.'}),
                chunk({}, finish_reason='stop'),
            ])

        with tempfile.TemporaryDirectory() as directory:
            with patch('joy.llm.Path.cwd', return_value=Path(directory)), self.client_patch(respond):
                chunks = [part async for part in stream_reply(
                    [{'role': 'user', 'content': 'Create it'}], self.settings(api=CHAT))]
            self.assertEqual(chunks, [
                'Working', ToolEvent('write', 'running'), ToolEvent('write', 'finished'),
                '\n\n', 'Created', ' it.',
            ])
            self.assertEqual((Path(directory) / 'hello.txt').read_text(), 'hello')
        self.assertEqual(len(payloads), 2)

    async def test_provider_request_and_errors(self):
        messages = [{'role': 'user', 'content': 'Hello'}]
        settings = self.settings()
        for status, mode in [(200, 'ok'), (401, 'error'), (400, 'error'), (404, 'error'),
                             (429, 'error'), (200, 'empty'), (200, 'truncated'),
                             (200, 'failed'), (200, 'incomplete')]:
            with self.subTest(status=status, mode=mode):
                def respond(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload['model'], DEFAULT_MODEL)
                    self.assertEqual(payload['input'], messages)
                    self.assertTrue(payload['stream'])
                    if status != 200:
                        return httpx.Response(status, json={'error': {
                            'message': 'private-key', 'code': 'unsupported_parameter', 'param': 'tools',
                        }})
                    events = [] if mode == 'empty' else [
                        {'type': 'response.output_text.delta', 'delta': 'Hello'},
                        {'type': 'response.output_text.delta', 'delta': ' back'},
                    ]
                    if mode != 'truncated':
                        events.append(completed(status=mode if mode in ('failed', 'incomplete') else 'completed'))
                    return sse(events)

                with self.client_patch(respond):
                    if mode == 'ok':
                        self.assertEqual([part async for part in stream_reply(messages, settings)],
                                         ['Hello', ' back'])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'model API could not') as error:
                            await collect_reply(messages, settings)
                        self.assertNotIn('private-key', str(error.exception))
                        if status != 200:
                            self.assertIn(f'HTTP {status}', str(error.exception))
                            self.assertIn('unsupported_parameter', str(error.exception))

    async def test_chat_request_and_errors(self):
        messages = [{'role': 'user', 'content': 'Hello'}]
        settings = self.settings(api=CHAT)
        for status, mode in [(200, 'ok'), (401, 'error'), (200, 'empty')]:
            with self.subTest(status=status, mode=mode):
                def respond(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload['model'], DEFAULT_MODEL)
                    self.assertEqual(payload['messages'], messages)
                    self.assertTrue(payload['stream'])
                    if status != 200:
                        return httpx.Response(status, json={'error': {
                            'message': 'private-key', 'code': 'invalid_api_key',
                        }})
                    chunks = [] if mode == 'empty' else [
                        chunk({'role': 'assistant', 'content': 'Hello back'}),
                        chunk({}, finish_reason='stop'),
                    ]
                    return stream_chunks(chunks)

                with self.client_patch(respond):
                    if mode == 'ok':
                        self.assertEqual([part async for part in stream_reply(messages, settings)],
                                         ['Hello back'])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'model API could not') as error:
                            await collect_reply(messages, settings)
                        self.assertNotIn('private-key', str(error.exception))
                        if status != 200:
                            self.assertIn(f'HTTP {status}', str(error.exception))

    async def test_incomplete_stream_does_not_execute_tool(self):
        call = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                'name': 'write', 'arguments': '{"path":"bad.txt","content":"bad"}'}
        with self.client_patch(lambda request: sse([
            {'type': 'response.output_item.done', 'output_index': 0, 'item': call},
        ])), patch('joy.llm.create_tools') as tools:
            tools.return_value.tools.return_value = []
            with self.assertRaises(RuntimeError):
                await collect_reply([{'role': 'user', 'content': 'write'}], self.settings())
            tools.return_value.execute.assert_not_called()

    async def test_incomplete_chat_tool_call_does_not_execute(self):
        with self.client_patch(lambda request: stream_chunks([
            chunk({'tool_calls': [{'index': 0, 'function': {'name': 'write', 'arguments': '{'}}]}),
            chunk({}, finish_reason='tool_calls'),
        ])), patch('joy.llm.create_tools') as tools:
            tools.return_value.tools.return_value = []
            with self.assertRaises(RuntimeError):
                await collect_reply([{'role': 'user', 'content': 'write'}], self.settings(api=CHAT))
            tools.return_value.execute.assert_not_called()

    async def test_cancel_does_not_wait_for_sync_request(self):
        started, release, finished = Event(), Event(), Event()

        def slow_client(**kwargs):
            started.set()
            try:
                release.wait(2)
                raise RuntimeError('late request failure')
            finally:
                finished.set()

        with patch('joy.llm.OpenAI', side_effect=slow_client):
            task = asyncio.create_task(collect_reply(
                [{'role': 'user', 'content': 'Hello'}], self.settings()))
            try:
                for _ in range(100):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(started.is_set())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=0.2)
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                while not finished.is_set():
                    await asyncio.sleep(0.01)
