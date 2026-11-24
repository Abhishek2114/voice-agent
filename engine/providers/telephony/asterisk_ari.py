"""Asterisk REST Interface (ARI) adapter — **integration-ready, not production-verified**.

Uses only documented ARI resources (https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/Asterisk_REST_Interface/):
  GET    /ari/asterisk/info
  POST   /ari/channels                       (originate into our Stasis app)
  POST   /ari/channels/{id}/answer
  DELETE /ari/channels/{id}
  POST   /ari/channels/externalMedia         (RTP media to/from this process)
  POST   /ari/bridges, POST /ari/bridges/{id}/addChannel, DELETE /ari/bridges/{id}
  WS     /ari/events?app=…&api_key=user:pass

Asterisk itself connects to your SIP trunk provider; this adapter never talks to a
carrier directly and contains no provider-specific endpoints or credentials.
Real calling is disabled unless explicitly enabled and authorised in settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from engine.providers.base import ProviderHealth
from engine.providers.telephony.base import (
    CallStatus,
    InboundHandler,
    TelephonyCall,
    TelephonyError,
)
from engine.providers.telephony.rtp import RtpMedia

log = logging.getLogger(__name__)

# Q.850 cause codes reported by ChannelDestroyed
_CAUSE_STATUS = {17: CallStatus.BUSY, 18: CallStatus.NO_ANSWER, 19: CallStatus.NO_ANSWER, 16: CallStatus.COMPLETED,
                 21: CallStatus.FAILED, 1: CallStatus.FAILED, 34: CallStatus.FAILED, 38: CallStatus.FAILED}  # fmt: skip
_NUMBER = re.compile(r"^\+?[0-9]{6,16}$")
_APP = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class AriConfig:
    base_url: str = "http://127.0.0.1:8088"
    username: str = ""
    password: str = ""
    app: str = "voice-agent"
    endpoint_template: str = "PJSIP/{number}@trunk"
    caller_id: str = ""
    rtp_bind_host: str = "127.0.0.1"
    rtp_advertise_host: str = "127.0.0.1"
    rtp_port_start: int = 40000
    rtp_port_end: int = 40100
    byte_order: str = "big"

    def validate(self) -> list[str]:
        errs = []
        u = urlparse(self.base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            errs.append("base_url must be an http(s) URL")
        if not self.username or not self.password:
            errs.append("ARI username and password are required")
        if not _APP.match(self.app):
            errs.append("app name may only contain letters, digits, - and _")
        if "{number}" not in self.endpoint_template:
            errs.append("endpoint_template must contain {number}")
        if not (1024 < self.rtp_port_start < self.rtp_port_end <= 65535):
            errs.append("invalid RTP port range")
        return errs


class AsteriskAriProvider:
    name = "asterisk_ari"
    is_real = True

    def __init__(self, config: AriConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport
        self._inbound: InboundHandler | None = None
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._calls: dict[str, TelephonyCall] = {}
        self._bridges: dict[str, str] = {}
        self._media_channels: dict[str, str] = {}
        self._own_channels: set[str] = set()
        self._events_task: asyncio.Task[None] | None = None
        self._next_port = config.rtp_port_start

    def validate_config(self) -> list[str]:
        return self.config.validate()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            auth=(self.config.username, self.config.password),
            timeout=10.0,
            transport=self._transport,
        )

    async def health(self) -> ProviderHealth:
        errs = self.validate_config()
        if errs:
            return ProviderHealth(self.name, "unavailable", "; ".join(errs), is_local=False)
        try:
            async with self._client() as c:
                r = await c.get("/ari/asterisk/info")
        except httpx.HTTPError as exc:
            return ProviderHealth(self.name, "unavailable", f"ARI not reachable: {exc.__class__.__name__}", is_local=False)
        if r.status_code == 401:
            return ProviderHealth(self.name, "unavailable", "ARI rejected the credentials", is_local=False)
        if r.status_code != 200:
            return ProviderHealth(self.name, "degraded", f"ARI HTTP {r.status_code}", is_local=False)
        info = r.json()
        version = (info.get("system") or {}).get("version")
        return ProviderHealth(self.name, "ok", f"Asterisk {version}", is_local=False, extra={"version": version})

    # ---------------------------------------------------------------- events
    async def ensure_events(self) -> None:
        if self._events_task and not self._events_task.done():
            return
        self._events_task = asyncio.create_task(self._event_loop())

    def events_url(self) -> str:
        u = urlparse(self.config.base_url)
        scheme = "wss" if u.scheme == "https" else "ws"
        key = quote(f"{self.config.username}:{self.config.password}", safe=":")
        return f"{scheme}://{u.netloc}/ari/events?app={quote(self.config.app)}&api_key={key}"

    async def _event_loop(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.events_url(), open_timeout=10) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        with contextlib.suppress(Exception):
                            await self.handle_event(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop must survive any socket error
                log.warning("ARI event socket error: %s (retrying in %.0fs)", exc.__class__.__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def handle_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        channel = ev.get("channel") or {}
        cid = channel.get("id")
        if not cid or cid in self._media_channels.values():
            return
        if kind == "StasisStart":
            if cid in self._waiters and not self._waiters[cid].done():
                self._waiters[cid].set_result(ev)
            elif cid not in self._own_channels and self._inbound:
                await self._accept_inbound(channel)
        elif kind == "ChannelDestroyed":
            if cid in self._waiters and not self._waiters[cid].done():
                self._waiters[cid].set_result(ev)
            call = self._calls.pop(cid, None)
            if call is not None:
                if call.status == CallStatus.ANSWERED:
                    call.transition(CallStatus.COMPLETED, ev.get("cause_txt") or "hangup")
                if call.media:
                    await call.media.close("remote_hangup")
                await self._teardown(cid)

    # ---------------------------------------------------------------- calls
    async def dial(self, to_number: str, from_number: str | None, *, ring_timeout_s: float = 30,
                   seed: str | None = None) -> TelephonyCall:  # fmt: skip
        errs = self.validate_config()
        if errs:
            raise TelephonyError("; ".join(errs), code="invalid_config")
        if not _NUMBER.match(to_number):
            raise TelephonyError("invalid destination number", code="invalid_number")
        await self.ensure_events()
        channel_id = f"va-{uuid.uuid4().hex}"
        call = TelephonyCall(self.name, channel_id, "outbound", to_number, from_number or self.config.caller_id)
        call.transition(CallStatus.DIALING)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[channel_id] = fut
        self._own_channels.add(channel_id)
        params: dict[str, str | int] = {
            "endpoint": self.config.endpoint_template.format(number=to_number),
            "app": self.config.app,
            "appArgs": "outbound",
            "channelId": channel_id,
            "timeout": int(ring_timeout_s),
        }
        if call.from_number:
            params["callerId"] = call.from_number
        try:
            async with self._client() as c:
                r = await c.post("/ari/channels", params=params)
            if r.status_code >= 400:
                call.transition(CallStatus.FAILED, f"originate HTTP {r.status_code}")
                return call
            call.transition(CallStatus.RINGING)
            ev = await asyncio.wait_for(fut, timeout=ring_timeout_s + 5)
        except TimeoutError:
            call.transition(CallStatus.NO_ANSWER, "ring timeout")
            await self._hangup_channel(channel_id)
            return call
        except httpx.HTTPError as exc:
            call.transition(CallStatus.FAILED, f"ARI error: {exc.__class__.__name__}")
            return call
        finally:
            self._waiters.pop(channel_id, None)
        if ev.get("type") == "ChannelDestroyed":
            status = _CAUSE_STATUS.get(int(ev.get("cause") or 0), CallStatus.FAILED)
            call.transition(status if status != CallStatus.COMPLETED else CallStatus.NO_ANSWER,
                            ev.get("cause_txt") or "")  # fmt: skip
            call.hangup_cause = str(ev.get("cause"))
            return call
        call.transition(CallStatus.ANSWERED)
        call.media = await self._attach_media(channel_id)
        self._calls[channel_id] = call
        return call

    async def _accept_inbound(self, channel: dict[str, Any]) -> None:
        cid = channel["id"]
        caller = (channel.get("caller") or {}).get("number") or "unknown"
        dialplan = (channel.get("dialplan") or {}).get("exten") or ""
        call = TelephonyCall(self.name, cid, "inbound", dialplan, caller)
        call.transition(CallStatus.RINGING)
        async with self._client() as c:
            await c.post(f"/ari/channels/{cid}/answer")
        call.transition(CallStatus.ANSWERED)
        call.media = await self._attach_media(cid)
        self._calls[cid] = call
        if self._inbound:
            await self._inbound(call)

    async def _attach_media(self, channel_id: str) -> RtpMedia:
        port = self._alloc_port()
        media = RtpMedia(self.config.rtp_bind_host, port, byte_order=self.config.byte_order)
        await media.start()
        async with self._client() as c:
            b = await c.post("/ari/bridges", params={"type": "mixing", "bridgeId": f"vab-{uuid.uuid4().hex}"})
            b.raise_for_status()
            bridge_id = b.json()["id"]
            m = await c.post(
                "/ari/channels/externalMedia",
                params={
                    "app": self.config.app,
                    "external_host": f"{self.config.rtp_advertise_host}:{port}",
                    "format": "slin16",
                    "encapsulation": "rtp",
                    "transport": "udp",
                    "direction": "both",
                },
            )
            m.raise_for_status()
            media_id = m.json()["id"]
            vars_ = m.json().get("channelvars") or {}
            if vars_.get("UNICASTRTP_LOCAL_ADDRESS") and vars_.get("UNICASTRTP_LOCAL_PORT"):
                media.remote = (vars_["UNICASTRTP_LOCAL_ADDRESS"], int(vars_["UNICASTRTP_LOCAL_PORT"]))
            await c.post(f"/ari/bridges/{bridge_id}/addChannel", params={"channel": f"{channel_id},{media_id}"})
        self._bridges[channel_id] = bridge_id
        self._media_channels[channel_id] = media_id
        return media

    def _alloc_port(self) -> int:
        port = self._next_port
        self._next_port += 2
        if self._next_port >= self.config.rtp_port_end:
            self._next_port = self.config.rtp_port_start
        return port

    async def _hangup_channel(self, channel_id: str) -> None:
        with contextlib.suppress(httpx.HTTPError):
            async with self._client() as c:
                await c.delete(f"/ari/channels/{channel_id}", params={"reason": "normal"})

    async def _teardown(self, channel_id: str) -> None:
        media_id = self._media_channels.pop(channel_id, None)
        bridge_id = self._bridges.pop(channel_id, None)
        with contextlib.suppress(httpx.HTTPError):
            async with self._client() as c:
                if media_id:
                    await c.delete(f"/ari/channels/{media_id}")
                if bridge_id:
                    await c.delete(f"/ari/bridges/{bridge_id}")

    async def hangup(self, call: TelephonyCall, reason: str = "normal") -> None:
        cid = call.provider_call_id
        await self._hangup_channel(cid)
        if call.media:
            await call.media.close(reason)
        await self._teardown(cid)
        self._calls.pop(cid, None)
        if call.status == CallStatus.ANSWERED:
            call.transition(CallStatus.COMPLETED, reason)

    def on_inbound(self, handler: InboundHandler) -> None:
        self._inbound = handler

    async def close(self) -> None:
        if self._events_task:
            self._events_task.cancel()
            with contextlib.suppress(BaseException):
                await self._events_task
