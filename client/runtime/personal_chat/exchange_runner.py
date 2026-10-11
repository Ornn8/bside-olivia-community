"""Keep exchange work alive across socket replacement, with one send lease."""
import asyncio


class ExchangeRunner:
    def __init__(self, handler):
        self.handler = handler
        self.sender = None
        self.tasks = {}
        self.stopped = False
        for name in ('ingest', 'pending', 'is_control_message', 'handle_control'):
            value = getattr(handler, name, None)
            if value is not None:
                setattr(self, name, value)
        self.send = self._sender()

    def _sender(self, event=None):
        def lease():
            sender = self.sender
            if self.stopped or sender is None or not sender.is_available():
                raise RuntimeError('PERSONAL_CHAT_CHANNEL_DISCONNECTED')
            return sender.for_exchange(event) if event is not None else sender

        async def send(text):
            return await lease()(text)

        def action(name):
            async def deliver(*args, **kwargs):
                return await getattr(lease(), name)(*args, **kwargs)
            return deliver

        for name in ('audio', 'image', 'video', 'file', 'resolve_media'):
            setattr(send, name, action(name))
        send.is_available = lambda: (not self.stopped and self.sender is not None
                                     and self.sender.is_available())
        send.for_exchange = self._sender
        return send

    def ready(self, channel, sender):
        if self.stopped:
            return
        self.sender = sender
        ready = getattr(self.handler, 'ready', None)
        if callable(ready):
            ready(channel, self.send)

    def disconnected(self, sender):
        if self.sender is sender:
            self.sender = None

    async def __call__(self, event, sender):
        if self.stopped:
            return
        key = event.exchange_id
        task = self.tasks.get(key)
        if task is None:
            task = asyncio.create_task(self.handler(event, self.send))
            self.tasks[key] = task

            def finished(done):
                if self.tasks.get(key) is done:
                    self.tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        await asyncio.shield(task)

    async def close(self):
        self.stopped = True
        self.sender = None
        tasks = tuple(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
