# Copyright (C) 2020 - 2021 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import json
from collections import deque

import asyncio

from aiohttp import web
from aiohttp_sse import sse_response

from cuckoo.common.machines import serialize_machinelists
from cuckoo.common.storage import (
    random_filename, delete_file, Paths, AnalysisPaths, split_analysis_id,
    create_analysis_folder, TaskPaths, TASK_ID_REGEX
)
from cuckoo.common.importing import ZippedNodeWork, AnalysisImportError

from .node import InfoStreamReceiver, NodeError, NodeMsgTypes


MAX_UPLOAD_SIZE = 1024 * 1024 * 1024

class StateSSE(InfoStreamReceiver):

    BACKLOG_SIZE = 100

    def __init__(self, loop):
        self.loop = loop
        self.queues = set()
        self.backlog = deque(maxlen=self.BACKLOG_SIZE)
        self.cur_id = 1
        self._streamlock = asyncio.Lock()

    async def stream_event(self, data):
        async with self._streamlock:
            curid = self.cur_id + 1
            self.cur_id = curid

            id_data = (curid, data)
            self.backlog.append(id_data)

            for q in self.queues:
                await q.put(id_data)

    async def get_events_since(self, last_id):
        async with self._streamlock:
            for event_id, data in self.backlog:
                if event_id <= last_id:
                    continue

                yield event_id, data

    async def get_stream(self, last_id=None):
        q = asyncio.Queue()
        if last_id:
            async for event_id, data in self.get_events_since(last_id):
                q.put_nowait((event_id, data))

        self.queues.add(q)
        return q

    def remove_stream(self, q):
        self.queues.discard(q)

    def _add_stream_data(self, data):
        asyncio.run_coroutine_threadsafe(self.stream_event(data), self.loop)

    def task_state(self, task_id, state):
        self._add_stream_data(json.dumps({
            "type": NodeMsgTypes.TASK_STATE, "task_id": task_id, "state": state
        }))

class API:

    def __init__(self, nodectx, eventstreamer):
        self.ctx = nodectx
        self.streamer = eventstreamer
        self._analyses_lock = asyncio.Lock()
        self._analysis_locks = {}

    async def get_lock(self, analysis_id):
        async with self._analyses_lock:
            return self._analysis_locks.setdefault(analysis_id, asyncio.Lock())

    async def ping(self, request):
        return web.Response()

    async def get_machines(self, request):
        return web.json_response(
            serialize_machinelists(self.ctx.machinery_manager.machines)
        )

    async def get_eventstream(self, request):
        try:
            last_id = int(request.headers.get("Last-Event-Id"))
        except (TypeError, ValueError):
            last_id = None

        async with sse_response(request) as resp:
            stream = await self.streamer.get_stream(last_id=last_id)
            try:
                while not resp.task.done():
                    event_id, data = await stream.get()
                    await resp.send(data, id=event_id)
                    stream.task_done()
            finally:
                self.streamer.remove_stream(stream)

            return resp

    async def upload_work(self, request):
        reader = await request.multipart()
        field = await reader.next()
        if field.name != "file":
            return web.json_response({"error": "Bad field name"}, status=400)

        filename = random_filename("zip")
        zippath = Paths.importables(filename)

        size = 0
        with open(zippath, "wb") as fp:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break

                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    fp.close()
                    delete_file(zippath)
                    return web.json_response(
                        {"error": "File too large"}, status=400
                    )

                fp.write(chunk)

        with ZippedNodeWork(zippath) as nodework:
            try:
                analysis_path = AnalysisPaths.path(nodework.analysis.id)

                lock = await self.get_lock(nodework.analysis.id)
                async with lock:
                    # Create the analysis dir if it does not exist. This means
                    # no other tasks of this analysis have been unpacked here
                    # yet.
                    if not analysis_path.exists():
                        date, identifier = split_analysis_id(
                            nodework.analysis.id
                        )
                        create_analysis_folder(date, identifier)
                        nodework.unzip(analysis_path, task_only=False)
                    else:
                        # Only unpack the task work folder contents if the
                        # analysis folder already exists.
                        nodework.unzip(analysis_path, task_only=True)
            except AnalysisImportError as e:
                return web.json_response(
                    {"error": f"Work unpacking failed: {e}"}, status=400
                )

            finally:
                nodework.delete()

        return web.Response()

    async def start_task(self, request):
        try:
            data = await request.json()
        except json.JSONDecodeError as e:
            return web.json_response(
                {"error": f"Invalid JSON data: {e}"}, status=400
            )

        machine_name = data.get("machine_name")
        if not machine_name or not isinstance(machine_name, str):
            return web.json_response(
                {"error": f"Missing or invalid machine_name"}, status=400
            )

        task_id = request.match_info["task_id"]
        if not TaskPaths.path(task_id).exists():
            return web.json_response(
                {"error", "Unknown task"}, status=404
            )

        try:
            self.ctx.node.add_work(task_id, machine_name)
        except NodeError as e:
            return web.json_response(
                {"error": f"Task cannot be started: {e}"}, status=400
            )

        return web.Response()

    async def task_result(self, request):
        task_id = request.match_info["task_id"]
        try:
            zipped_result = TaskPaths.zipped_results(task_id)
        except ValueError:
            return web.HTTPBadRequest()

        if not zipped_result.exists():
            return web.HTTPNotFound()

        return web.FileResponse(path=zipped_result)

class APIRunner:

    def __init__(self, runner, loop, statesse):
        self.runner = runner
        self.loop = loop
        self.statesse = statesse
        self.site = None

    async def _stop(self):
        await self.runner.shutdown()
        await self.site.stop()
        await self.runner.cleanup()

    def stop(self):
        self.loop.stop()
        asyncio.run_coroutine_threadsafe(self._stop(), self.loop)

    def create_site(self, host="localhost", port=8080):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.runner.setup())
        self.site = web.TCPSite(self.runner, host=host, port=port)
        self.loop.run_until_complete(self.site.start())

    def run_forever(self):
        self.loop.run_forever()

def make_api_runner(nodectx):
    loop = asyncio.new_event_loop()
    statesse = StateSSE(loop)
    api = API(nodectx, statesse)
    app = web.Application()
    app.add_routes([
        web.get("/ping", api.ping),
        web.get("/machines", api.get_machines),
        web.get("/events", api.get_eventstream),
        web.post("/uploadwork", api.upload_work),
        web.post(f"/task/{{task_id:{TASK_ID_REGEX}}}/start", api.start_task),
        web.get(f"/task/{{task_id:{TASK_ID_REGEX}}}", api.task_result),
    ])

    runner = web.AppRunner(app)
    return APIRunner(runner, loop, statesse)