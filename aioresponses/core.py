# -*- coding: utf-8 -*-
import asyncio
from collections import namedtuple
from functools import wraps
from typing import Callable, Dict, Tuple, Union, Optional, List  # noqa
from unittest.mock import patch

from aiohttp import (
    ClientConnectionError,
    ClientSession,
    hdrs,
    http
)

from .compat import (
    URL,
    Pattern,
    merge_params,
    normalize_url,
    build_response,
)


class RequestMatch(object):
    url_or_pattern = None  # type: Union[URL, Pattern]

    def __init__(self, url: Union[str, Pattern],
                 method: str = hdrs.METH_GET,
                 status: int = 200,
                 body: str = '',
                 payload: Dict = None,
                 exception: 'Exception' = None,
                 headers: Dict = None,
                 content_type: str = 'application/json',
                 response_class: 'ClientResponse' = None,
                 timeout: bool = False,
                 repeat: bool = False,
                 reason: Optional[str] = None,
                 callback: Optional[Callable] = None):
        if isinstance(url, Pattern):
            self.url_or_pattern = url
            self.match_func = self.match_regexp
        else:
            self.url_or_pattern = normalize_url(url)
            self.match_func = self.match_str
        self.method = method.lower()
        self.status = status
        self.body = body
        self.payload = payload
        self.exception = exception
        if timeout:
            self.exception = asyncio.TimeoutError('Connection timeout test')
        self.headers = headers
        self.content_type = content_type
        self.response_class = response_class
        self.repeat = repeat
        self.reason = reason
        if self.reason is None:
            try:
                self.reason = http.RESPONSES[self.status][0]
            except (IndexError, KeyError):
                self.reason = ''
        self.callback = callback

    def match_str(self, url: URL) -> bool:
        return self.url_or_pattern == url

    def match_regexp(self, url: URL) -> bool:
        return bool(self.url_or_pattern.match(str(url)))

    def match(self, method: str, url: URL) -> bool:
        if self.method != method.lower():
            return False
        return self.match_func(url)

    async def _build_response(
            self, url: URL, **kwargs: Dict
    ) -> 'Union[ClientResponse, Exception]':
        if isinstance(self.exception, Exception):
            return self.exception
        if callable(self.callback):
            resp = self.callback(url, **kwargs)
        else:
            resp = None
        if resp is None:
            resp = build_response(
                url=url,
                method=self.method,
                status=self.status,
                body=self.body,
                content_type=self.content_type,
                payload=self.payload,
                headers=self.headers,
                response_class=self.response_class,
                reason=self.reason)
        return resp


RequestCall = namedtuple('RequestCall', ['args', 'kwargs'])


class aioresponses(object):
    """Mock aiohttp requests made by ClientSession."""
    _matches = None  # type: List[RequestMatch]
    _responses = None  # type: List[ClientResponse]
    requests = None  # type: Dict

    def __init__(self, **kwargs):
        self._param = kwargs.pop('param', None)
        self._passthrough = kwargs.pop('passthrough', [])
        self.patcher = patch('aiohttp.client.ClientSession._request',
                             side_effect=self._request_mock,
                             autospec=True)
        self.requests = {}

    def __enter__(self) -> 'aioresponses':
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def __call__(self, f):
        def _pack_arguments(ctx, *args, **kwargs) -> Tuple[Tuple, Dict]:
            if self._param:
                kwargs[self._param] = ctx
            else:
                args += (ctx,)
            return args, kwargs

        if asyncio.iscoroutinefunction(f):
            @wraps(f)
            async def wrapped(*args, **kwargs):
                with self as ctx:
                    args, kwargs = _pack_arguments(ctx, *args, **kwargs)
                    return await f(*args, **kwargs)
        else:
            @wraps(f)
            def wrapped(*args, **kwargs):
                with self as ctx:
                    args, kwargs = _pack_arguments(ctx, *args, **kwargs)
                    return f(*args, **kwargs)
        return wrapped

    def clear(self):
        self._responses.clear()
        self._matches.clear()

    def start(self):
        self._responses = []
        self._matches = []
        self.patcher.start()
        self.patcher.return_value = self._request_mock

    def stop(self) -> None:
        for response in self._responses:
            response.close()
        self.patcher.stop()
        self.clear()

    def head(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_HEAD, **kwargs)

    def get(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_GET, **kwargs)

    def post(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_POST, **kwargs)

    def put(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_PUT, **kwargs)

    def patch(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_PATCH, **kwargs)

    def delete(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_DELETE, **kwargs)

    def options(self, url: 'Union[URL, str]', **kwargs):
        self.add(url, method=hdrs.METH_OPTIONS, **kwargs)

    def add(self, url: 'Union[URL, str]', method: str = hdrs.METH_GET,
            status: int = 200,
            body: str = '',
            exception: 'Exception' = None,
            content_type: str = 'application/json',
            payload: Dict = None,
            headers: Dict = None,
            response_class: 'ClientResponse' = None,
            repeat: bool = False,
            timeout: bool = False,
            reason: Optional[str] = None,
            callback: Optional[Callable] = None) -> None:
        self._matches.append(RequestMatch(
            url,
            method=method,
            status=status,
            content_type=content_type,
            body=body,
            exception=exception,
            payload=payload,
            headers=headers,
            response_class=response_class,
            repeat=repeat,
            timeout=timeout,
            reason=reason,
            callback=callback,
        ))

    async def match(
            self, method: str, url: URL, **kwargs: Dict
    ) -> Optional['ClientResponse']:
        for i, matcher in enumerate(self._matches):
            if matcher.match(method, url):
                response = await matcher._build_response(url, **kwargs)
                break
        else:
            return None

        if matcher.repeat is False:
            del self._matches[i]
        if isinstance(response, Exception):
            raise response
        return response

    async def _request_mock(self, orig_self: ClientSession,
                            method: str, url: 'Union[URL, str]',
                            *args: Tuple,
                            **kwargs: Dict) -> 'ClientResponse':
        """Return mocked response object or raise connection error."""
        url = normalize_url(merge_params(url, kwargs.get('params')))
        url_str = str(url)
        for prefix in self._passthrough:
            if url_str.startswith(prefix):
                return (await self.patcher.temp_original(
                    orig_self, method, url, *args, **kwargs
                ))

        response = await self.match(method, url, **kwargs)
        if response is None:
            raise ClientConnectionError(
                'Connection refused: {} {}'.format(method, url)
            )
        self._responses.append(response)
        key = (method, url)
        self.requests.setdefault(key, [])
        self.requests[key].append(RequestCall(args, kwargs))
        return response
