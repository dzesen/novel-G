"""PyMongo access adapters sharing the backup/restore coordination boundary."""
from __future__ import annotations

import inspect

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.command_cursor import AsyncCommandCursor
from pymongo.asynchronous.cursor import AsyncCursor

from backend.db.maintenance import database_access, finish_database_io

_READ_METHODS = {
    "find_one", "count_documents", "estimated_document_count", "distinct",
    "index_information", "options", "list_collection_names", "list_collections",
    "list_indexes",
}
_CURSOR_TYPES = (AsyncCursor, AsyncCommandCursor)


def _wrap_result(value, key):
    if isinstance(value, _CURSOR_TYPES):
        return CoordinatedCursor(value, key)
    if isinstance(value, AsyncCollection):
        return CoordinatedCollection(value, key)
    return value


async def _call(method, key, write, args, kwargs):
    async with database_access(key=key, write=write):
        result = await finish_database_io(method(*args, **kwargs))
    return _wrap_result(result, key)


class CoordinatedCursor:
    def __init__(self, cursor, key):
        self._cursor, self._key = cursor, key

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await _call(self._cursor.__anext__, self._key, False, (), {})

    async def __aenter__(self):
        await self._cursor.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await _call(self._cursor.__aexit__, self._key, False, args, {})

    def __getattr__(self, name):
        value = getattr(self._cursor, name)
        if inspect.iscoroutinefunction(value):
            async def read(*args, **kwargs):
                return await _call(value, self._key, False, args, kwargs)
            return read
        if callable(value):
            def configure(*args, **kwargs):
                return _wrap_result(value(*args, **kwargs), self._key)
            return configure
        return value


class CoordinatedCollection:
    def __init__(self, collection, key):
        self._collection, self._key = collection, key

    async def update_one(self, *args, **kwargs):
        """Keep the injectable compare-and-set seam on the collection class."""
        return await _call(self._collection.update_one, self._key, True, args, kwargs)

    def __getattr__(self, name):
        if name == "database":
            return CoordinatedDatabase(self._collection.database, self._key)
        value = getattr(self._collection, name)
        if inspect.iscoroutinefunction(value):
            async def operation(*args, **kwargs):
                # Aggregate can contain $out/$merge; conservatively treat it as a write.
                return await _call(value, self._key, name not in _READ_METHODS, args, kwargs)
            return operation
        if callable(value):
            def configure(*args, **kwargs):
                return _wrap_result(value(*args, **kwargs), self._key)
            return configure
        return value


class CoordinatedDatabase:
    def __init__(self, database, key):
        self._database, self._key = database, key

    def __getitem__(self, name):
        return CoordinatedCollection(self._database[name], self._key)

    def get_collection(self, *args, **kwargs):
        return CoordinatedCollection(self._database.get_collection(*args, **kwargs), self._key)

    def with_options(self, *args, **kwargs):
        return CoordinatedDatabase(self._database.with_options(*args, **kwargs), self._key)

    def __getattr__(self, name):
        value = getattr(self._database, name)
        if isinstance(value, AsyncCollection):
            return CoordinatedCollection(value, self._key)
        if inspect.iscoroutinefunction(value):
            async def operation(*args, **kwargs):
                return await _call(value, self._key, name not in _READ_METHODS, args, kwargs)
            return operation
        return value
