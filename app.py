"""
Vaillant Dashboard - a small local web app to monitor and control a Vaillant
heating system through the myVAILLANT cloud (the same API the phone app uses).

Run:  python app.py            (uses config.toml)
      python app.py --demo     (fake data, no account needed)
Then open http://127.0.0.1:8321
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import math
import random
import sqlite3
import time
import tomllib
import webbrowser
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
log = logging.getLogger("vaillant-dashboard")

DEFAULT_CONFIG: dict[str, Any] = {
    "system": "myvaillant",  # myvaillant | vsmart
    "server": {"host": "127.0.0.1", "port": 8321, "open_browser": True},
    "vaillant": {
        "email": "",
        "password": "",
        "brand": "vaillant",
        "country": "",
        "poll_seconds": 120,
        "energy_refresh_minutes": 60,
    },
    "vsmart": {
        "email": "",
        "password": "",
        "client_id": "na_client_android_vaillant",
        "client_secret": "",
        "user_prefix": "vaillant",
        "app_version": "1.0.4.0",
    },
    "external_sensor": {
        "mode": "none",  # none | push | shelly | http_json
        "name": "External sensor",
        "url": "",
        "temperature_path": "temperature",
        "humidity_path": "humidity",
        "poll_seconds": 60,
        "max_age_minutes": 30,
    },
    "compensation": {
        "max_offset": 3.0,
        "min_minutes_between_changes": 15,
        "veto_hours": 2.0,
    },
}


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        with path.open("rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
            else:
                cfg[section] = values
    return cfg


def now_ts() -> float:
    return time.time()


def round_half(x: float) -> float:
    return round(x * 2) / 2


def enum_str(v) -> str | None:
    return None if v is None else str(v.value if hasattr(v, "value") else v)


def ago_text(ts: float) -> str:
    m = round((now_ts() - ts) / 60)
    return f"{m} min ago" if m < 120 else f"{m / 60:.1f} h ago"


def iso(d: dt.datetime | None) -> str | None:
    return d.isoformat() if d else None


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


# --------------------------------------------------------------------------- #
#  History storage                                                            #
# --------------------------------------------------------------------------- #


class History:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS readings (ts REAL, key TEXT, value REAL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_key_ts ON readings (key, ts)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, source TEXT, message TEXT, data TEXT)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")
        self.db.commit()

    def add_event(self, kind: str, source: str, message: str, data: dict | None = None):
        self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (now_ts(), kind, source, message, json.dumps(data, default=str) if data else None),
        )
        self.db.commit()

    def events(self, hours: float, kinds: list[str] | None = None, limit: int = 500) -> list[dict]:
        sql = "SELECT ts, kind, source, message, data FROM events WHERE ts >= ?"
        args: list = [now_ts() - hours * 3600]
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [
            {"ts": ts, "kind": k, "source": src, "message": msg, "data": json.loads(d) if d else None}
            for ts, k, src, msg, d in self.db.execute(sql, args)
        ]

    def add(self, values: dict[str, float | None], ts: float | None = None):
        ts = ts or now_ts()
        rows = [(ts, k, float(v)) for k, v in values.items() if v is not None]
        if rows:
            self.db.executemany("INSERT INTO readings VALUES (?, ?, ?)", rows)
            self.db.commit()

    def query(self, hours: float) -> dict[str, list[list[float]]]:
        since = now_ts() - hours * 3600
        out: dict[str, list[list[float]]] = {}
        for ts, key, value in self.db.execute(
            "SELECT ts, key, value FROM readings WHERE ts >= ? ORDER BY ts", (since,)
        ):
            out.setdefault(key, []).append([ts * 1000, value])
        return out

    def export_csv(self, hours: float) -> str:
        since = now_ts() - hours * 3600
        lines = ["timestamp,key,value"]
        for ts, key, value in self.db.execute(
            "SELECT ts, key, value FROM readings WHERE ts >= ? ORDER BY ts", (since,)
        ):
            lines.append(f"{dt.datetime.fromtimestamp(ts).isoformat()},{key},{value}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
#  Real backend: myVAILLANT cloud via myPyllant                               #
# --------------------------------------------------------------------------- #


def zone_schedule_setpoint(zone) -> float | None:
    """What the zone's own schedule asks for right now (ignoring quick veto)."""
    h = zone.heating
    mode = enum_str(h.operation_mode_heating)
    if mode == "OFF":
        return None
    if mode in ("MANUAL", "DAY"):
        return h.manual_mode_setpoint_heating or h.day_temperature_heating
    if mode == "SET_BACK":
        return h.set_back_temperature
    tp = h.time_program_heating
    if tp is None:
        return zone.desired_room_temperature_setpoint
    now = dt.datetime.now(zone.timezone)
    minute = now.hour * 60 + now.minute
    for slot in getattr(tp, WEEKDAYS[now.weekday()], []) or []:
        if slot.start_time <= minute < slot.end_time:
            return slot.setpoint or h.day_temperature_heating or h.manual_mode_setpoint_heating
    return h.set_back_temperature


class VaillantBackend:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api = None
        self.systems: list = []
        self.raw: list[dict] = []

    async def start(self):
        from myPyllant.api import MyPyllantAPI

        c = self.cfg
        if not c["email"] or not c["password"]:
            raise RuntimeError("Fill in email and password in config.toml (or run with --demo)")
        self.api = MyPyllantAPI(c["email"], c["password"], c["brand"], c["country"] or None)
        await self.api.login()
        log.info("Logged in to myVAILLANT as %s", c["email"])

    async def stop(self):
        if self.api and not self.api.aiohttp_session.closed:
            await self.api.aiohttp_session.close()

    async def _ensure_session(self):
        expires = self.api.oauth_session_expires
        if expires and expires - dt.datetime.now(dt.timezone.utc) < dt.timedelta(minutes=5):
            try:
                await self.api.refresh_token()
                log.info("Refreshed myVAILLANT token")
            except Exception:
                log.warning("Token refresh failed, logging in again", exc_info=True)
                await self.api.login()

    async def fetch(self) -> list[dict]:
        await self._ensure_session()
        systems = [
            s
            async for s in self.api.get_systems(
                include_connection_status=True,
                include_diagnostic_trouble_codes=True,
                include_rts=True,
                include_mpc=True,
                include_energy_management=True,
                include_ambisense_rooms=True,
            )
        ]
        self.systems = systems
        self.raw = []
        for s in systems:
            try:
                self.raw.append(s.prepare_dict())
            except Exception as e:  # never let the raw dump break the dashboard
                self.raw.append({"id": s.id, "error": f"could not serialise: {e}"})
        return [self._summarise(s) for s in systems]

    def _summarise(self, s) -> dict:
        zones = []
        for z in s.zones:
            c = z.associated_circuit
            modes = (
                ["AUTO", "DAY", "SET_BACK", "OFF"]
                if z.control_identifier.is_vrc700
                else ["TIME_CONTROLLED", "MANUAL", "OFF"]
            )
            zones.append(
                {
                    "index": z.index,
                    "name": (z.name or f"Zone {z.index}").strip(),
                    "active": z.is_active,
                    "room_temperature": z.current_room_temperature,
                    "humidity": z.current_room_humidity,
                    # 0.0 means "eco / no heating demand" on some controllers
                    "target": (z.desired_room_temperature_setpoint_heating
                    or z.desired_room_temperature_setpoint) or None,
                    "schedule_setpoint": zone_schedule_setpoint(z),
                    "mode": enum_str(z.heating.operation_mode_heating),
                    "modes": modes,
                    "special_function": enum_str(z.current_special_function),
                    "heating_state": enum_str(z.heating_state),
                    "setback_temperature": z.heating.set_back_temperature,
                    "manual_setpoint": z.heating.manual_mode_setpoint_heating,
                    "quick_veto_end": iso(z.quick_veto_end_date_time) if z.quick_veto_ongoing else None,
                    "holiday_start": iso(z.general.holiday_start_date_time),
                    "holiday_end": iso(z.general.holiday_end_date_time),
                    "circuit": None
                    if c is None
                    else {
                        "index": c.index,
                        "state": enum_str(c.circuit_state),
                        "flow_temperature": c.current_circuit_flow_temperature,
                        "flow_setpoint": c.heating_circuit_flow_setpoint,
                        "heating_curve": c.heating_curve,
                        "flow_min": c.heating_flow_temperature_minimum_setpoint,
                        "flow_max": c.heating_flow_temperature_maximum_setpoint,
                        "summer_limit": c.heat_demand_limited_by_outside_temperature,
                    },
                }
            )
        dhw = [
            {
                "index": d.index,
                "temperature": d.current_dhw_temperature,
                "setpoint": d.tapping_setpoint,
                "min": d.min_setpoint,
                "max": d.max_setpoint,
                "mode": enum_str(d.operation_mode_dhw),
                "modes": ["AUTO", "DAY", "OFF"]
                if d.control_identifier.is_vrc700
                else ["TIME_CONTROLLED", "MANUAL", "OFF"],
                "special_function": enum_str(d.current_special_function),
            }
            for d in s.domestic_hot_water
        ]
        devices = []
        for d in s.devices:
            devices.append(
                {
                    "name": d.name_display,
                    "product": d.product_name,
                    "type": d.device_type,
                    "serial": d.device_serial_number,
                    "article": d.article_number,
                    "ebus_id": d.ebus_id,
                    "first_data": iso(d.first_data),
                    "last_data": iso(d.last_data),
                    "operational_data": d.operational_data,
                    "rts": d.rts_statistics,
                    "mpc": d.mpc,
                    "trouble_codes": d.diagnostic_trouble_codes,
                }
            )
        home = s.home
        return {
            "id": s.id,
            "name": home.home_name or home.nomenclature,
            "controller": enum_str(s.control_identifier),
            "connected": s.connected,
            "firmware": home.firmware_version,
            "gateway": home.product_information,
            "outdoor_temperature": s.outdoor_temperature,
            "outdoor_temperature_24h": s.outdoor_temperature_average_24h,
            "water_pressure": s.water_pressure,
            "system_flow_temperature": s.system_flow_temperature,
            "cylinder_top": s.cylinder_temperature_sensor_top_dhw,
            "cylinder_bottom": s.cylinder_temperature_sensor_bottom_dhw,
            "energy_manager_state": enum_str(s.energy_manager_state),
            "trouble_codes": s.diagnostic_trouble_codes,
            "zones": zones,
            "dhw": dhw,
            "devices": devices,
            "ambisense_rooms": [
                {
                    "index": r.room_index,
                    "name": r.room_configuration.name,
                    "temperature": r.room_configuration.current_temperature,
                    "humidity": r.room_configuration.current_humidity,
                    "setpoint": r.room_configuration.temperature_setpoint,
                    "window_open": r.room_configuration.window_state,
                }
                for r in s.ambisense_rooms
            ],
        }

    async def energy(self) -> list[dict]:
        from myPyllant.enums import DeviceDataBucketResolution

        series = []
        for s in self.systems:
            for device in s.devices:
                end = dt.datetime.now(device.timezone)
                start = (end - dt.timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
                async for data in self.api.get_data_by_device(
                    device, DeviceDataBucketResolution.DAY, start, end
                ):
                    if not data.data:
                        continue
                    series.append(
                        {
                            "device": device.name_display,
                            "operation_mode": data.operation_mode,
                            "energy_type": data.energy_type,
                            "unit": "Wh",
                            "total": sum(b.value or 0 for b in data.data),
                            "points": [
                                [b.start_date.date().isoformat(), b.value] for b in data.data
                            ],
                        }
                    )
        return series

    # -- controls ------------------------------------------------------------

    def _system(self, sid: str):
        for s in self.systems:
            if s.id == sid:
                return s
        raise HTTPException(404, f"Unknown system {sid}")

    def _zone(self, sid: str, idx: int):
        for z in self._system(sid).zones:
            if z.index == idx:
                return z
        raise HTTPException(404, f"Unknown zone {idx}")

    def _dhw(self, sid: str, idx: int):
        for d in self._system(sid).domestic_hot_water:
            if d.index == idx:
                return d
        raise HTTPException(404, f"Unknown hot water {idx}")

    async def act(self, a: str, p: dict):
        api = self.api
        await self._ensure_session()
        sid = p.get("system")
        if a == "zone_quick_veto":
            await api.quick_veto_zone_temperature(
                self._zone(sid, p["zone"]), float(p["temperature"]), float(p.get("hours") or 3)
            )
        elif a == "zone_cancel_veto":
            await api.cancel_quick_veto_zone_temperature(self._zone(sid, p["zone"]))
        elif a == "zone_mode":
            await api.set_zone_operating_mode(self._zone(sid, p["zone"]), p["mode"])
        elif a == "zone_manual_setpoint":
            await api.set_manual_mode_setpoint(self._zone(sid, p["zone"]), float(p["temperature"]))
        elif a == "zone_setback":
            await api.set_set_back_temperature(self._zone(sid, p["zone"]), float(p["temperature"]))
        elif a == "dhw_temperature":
            await api.set_domestic_hot_water_temperature(self._dhw(sid, p["dhw"]), round(float(p["temperature"])))
        elif a == "dhw_mode":
            await api.set_domestic_hot_water_operation_mode(self._dhw(sid, p["dhw"]), p["mode"])
        elif a == "dhw_boost":
            await api.boost_domestic_hot_water(self._dhw(sid, p["dhw"]))
        elif a == "dhw_cancel_boost":
            await api.cancel_hot_water_boost(self._dhw(sid, p["dhw"]))
        elif a == "holiday":
            system = self._system(sid)
            start = dt.datetime.now(system.timezone)
            end = start + dt.timedelta(days=float(p.get("days") or 7))
            setpoint = float(p.get("temperature") or 10) if system.control_identifier.is_vrc700 else None
            await api.set_holiday(system, start, end, setpoint)
        elif a == "cancel_holiday":
            await api.cancel_holiday(self._system(sid))
        else:
            raise HTTPException(400, f"Unknown action {a}")


# --------------------------------------------------------------------------- #
#  vSMART / eRELAX backend: older Netatmo-made thermostat with its own app    #
# --------------------------------------------------------------------------- #


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")} | (
            {"id": obj.id} if hasattr(obj, "id") else {}
        )
    return str(obj)


class VsmartBackend:
    """Talks to the Netatmo cloud that the vSMART app uses (via vaillant-netatmo-api)."""

    def __init__(self, cfg: dict, data_dir: Path):
        self.cfg = cfg
        self.token_path = data_dir / "vsmart_token.json"
        self.client = None
        self.http = None
        self.homes: list = []
        self.devices: list = []
        self.raw: list[dict] = []

    def _save_token(self, token):
        self.token_path.write_text(token.serialize())

    async def start(self):
        from httpx import AsyncClient
        from vaillant_netatmo_api import AuthClient, ThermostatClient, Token, TokenStore

        c = self.cfg
        if not c["client_secret"]:
            raise RuntimeError("Fill in client_secret in the [vsmart] section of config.toml (see README)")
        self.http = AsyncClient()
        token = Token.deserialize(self.token_path.read_text()) if self.token_path.exists() else None
        store = TokenStore(c["client_id"], c["client_secret"], token, self._save_token)
        if token is None:
            if not c["email"] or not c["password"]:
                raise RuntimeError("Fill in email and password in the [vsmart] section of config.toml")
            await AuthClient(self.http, store).async_token(
                c["email"], c["password"], c["user_prefix"], c["app_version"]
            )
            log.info("Logged in to vSMART as %s", c["email"])
        self.client = ThermostatClient(self.http, store)

    async def stop(self):
        if self.http:
            await self.http.aclose()

    async def fetch(self) -> list[dict]:
        from vaillant_netatmo_api import RequestUnauthorizedException

        try:
            self.homes = await self.client.async_get_homes_data()
            self.devices = await self.client.async_get_thermostats_data()
        except RequestUnauthorizedException:
            # stored token no longer valid: log in again with the password next round
            self.token_path.unlink(missing_ok=True)
            await self.stop()
            await self.start()
            raise
        self.raw = [{"homes": to_jsonable(self.homes), "devices": to_jsonable(self.devices)}]
        return [self._summarise(d) for d in self.devices]

    def _home_room(self):
        # The vSMART system has one home with one room (same assumption as the Home Assistant integration)
        if not self.homes or not self.homes[0].rooms:
            raise HTTPException(502, "vSMART returned no home/room to control")
        home = self.homes[0]
        return home, home.rooms[0]

    def _summarise(self, d) -> dict:
        from vaillant_netatmo_api import SystemMode

        home = self.homes[0] if self.homes else None
        zones = []
        for i, m in enumerate(d.modules):
            program = next((p for p in m.therm_program_list if p.selected), None)
            active = program.get_active_zone() if program else None
            night = next((z for z in (program.zones if program else []) if z.id == 1), None)
            away_on = m.setpoint_away.setpoint_activate
            manual_on = m.setpoint_manual.setpoint_activate
            if d.system_mode == SystemMode.FROSTGUARD:
                mode = "FROST_GUARD"
            elif d.system_mode == SystemMode.SUMMER:
                mode = "HOT_WATER_ONLY"
            elif away_on:
                mode = "AWAY"
            else:
                mode = "SCHEDULE"
            zones.append(
                {
                    "index": i,
                    "name": m.module_name or "Thermostat",
                    "active": True,
                    "room_temperature": m.measured.temperature,
                    "humidity": None,
                    "target": m.measured.est_setpoint_temp
                    if m.measured.est_setpoint_temp is not None
                    else m.measured.setpoint_temp,
                    "schedule_setpoint": active.temp if active and mode == "SCHEDULE" else None,
                    "mode": mode,
                    "modes": ["SCHEDULE", "AWAY", "HOT_WATER_ONLY", "FROST_GUARD"],
                    "controls": ["veto", "mode", "holiday"],
                    "special_function": "HOLIDAY" if away_on else "QUICK_VETO" if manual_on else "NONE",
                    "heating_state": "HEATING_UP" if m.boiler_status else "IDLE",
                    "setback_temperature": night.temp if night else None,
                    "manual_setpoint": None,
                    "quick_veto_end": iso(m.setpoint_manual.setpoint_endtime.astimezone())
                    if manual_on and m.setpoint_manual.setpoint_endtime
                    else None,
                    "holiday_start": None,
                    "holiday_end": iso(m.setpoint_away.setpoint_endtime.astimezone())
                    if away_on and m.setpoint_away.setpoint_endtime
                    else None,
                    "circuit": None,
                    "extra": {
                        "Schedule": f"{program.name} ({active.name if active else '–'})" if program else "–",
                        "Battery": f"{m.battery_percent} %",
                        "Radio signal": m.rf_status,
                    },
                }
            )
        return {
            "id": d.id,
            "name": (home.name if home and home.name else d.station_name) or "Heating",
            "controller": "vSMART",
            "connected": None,
            "firmware": str(d.firmware),
            "gateway": "vSMART relay",
            "outdoor_temperature": d.outdoor_temperature.te,
            "outdoor_temperature_24h": None,
            "water_pressure": None,
            "system_flow_temperature": None,
            "cylinder_top": None,
            "cylinder_bottom": None,
            "energy_manager_state": None,
            "trouble_codes": [],
            "zones": zones,
            "dhw": []
            if d.dhw is None
            else [
                {
                    "index": 0,
                    "temperature": None,
                    "setpoint": d.dhw,
                    "min": d.dhw_min,
                    "max": d.dhw_max,
                    "mode": None,
                    "modes": [],
                    "special_function": "CYLINDER_BOOST" if d.setpoint_hwb.setpoint_activate else "REGULAR",
                }
            ],
            "devices": [
                {
                    "name": d.station_name or "Relay",
                    "product": "vSMART relay",
                    "type": d.type,
                    "serial": d.id,
                    "article": None,
                    "ebus_id": None,
                    "first_data": None,
                    "last_data": None,
                    "operational_data": {"wifi_signal": d.wifi_status, "system_mode": enum_str(d.system_mode)},
                    "rts": None,
                    "mpc": None,
                    "trouble_codes": [],
                }
            ]
            + [
                {
                    "name": m.module_name or "Thermostat",
                    "product": "vSMART thermostat",
                    "type": m.type,
                    "serial": m.id,
                    "article": None,
                    "ebus_id": None,
                    "first_data": None,
                    "last_data": None,
                    "operational_data": {"battery": f"{m.battery_percent} %", "radio_signal": m.rf_status,
                                         "firmware": m.firmware},
                    "rts": None,
                    "mpc": None,
                    "trouble_codes": [],
                }
                for m in d.modules
            ],
            "ambisense_rooms": [],
        }

    async def energy(self) -> list[dict]:
        from vaillant_netatmo_api import MeasurementScale, MeasurementType

        series = []
        start = dt.datetime.now() - dt.timedelta(days=14)
        for d in self.devices:
            for m in d.modules:
                for mtype in (
                    MeasurementType.SUM_ENERGY_GAS_HEATING,
                    MeasurementType.SUM_ENERGY_GAS_WATER,
                    MeasurementType.SUM_ENERGY_ELEC_HEATING,
                    MeasurementType.SUM_ENERGY_ELEC_WATER,
                ):
                    try:
                        items = await self.client.async_get_measure(d.id, m.id, mtype, MeasurementScale.DAY, start)
                    except Exception as e:
                        log.debug("No %s data: %s", mtype.value, e)
                        continue
                    points = []
                    for item in items:
                        for n, v in enumerate(item.value):
                            day = dt.datetime.fromtimestamp(item.beg_time + n * (item.step_time or 86400))
                            points.append([day.date().isoformat(), v])
                    if any(v for _, v in points):
                        heating = "HEATING" in mtype.name
                        series.append(
                            {
                                "device": m.module_name or d.station_name,
                                "operation_mode": "HEATING" if heating else "DOMESTIC_HOT_WATER",
                                "energy_type": "GAS" if "GAS" in mtype.name else "ELECTRICITY",
                                "unit": "Wh",
                                "total": sum(v or 0 for _, v in points),
                                "points": points,
                            }
                        )
        return series

    async def act(self, a: str, p: dict):
        from vaillant_netatmo_api import TemperatureControlMode, ThermMode, ThermSetpointMode

        home, room = self._home_room()
        device = next((d for d in self.devices if d.id == p.get("system")), None)
        if device is None:
            raise HTTPException(404, f"Unknown system {p.get('system')}")
        c = self.client
        if a == "zone_quick_veto":
            end = dt.datetime.now() + dt.timedelta(hours=float(p.get("hours") or 3))
            await c.async_set_state_for_room(
                home.id, room.id, ThermSetpointMode.MANUAL,
                therm_setpoint_temperature=float(p["temperature"]), therm_setpoint_end_time=end,
            )
        elif a == "zone_cancel_veto":
            await c.async_set_state_for_room(home.id, room.id, ThermSetpointMode.HOME)
        elif a == "zone_mode":
            mode = p["mode"]
            if mode == "SCHEDULE":
                await c.async_set_home_data(home.id, TemperatureControlMode.HEATING, ThermMode.SCHEDULE)
            elif mode == "AWAY":
                await c.async_set_home_data(home.id, TemperatureControlMode.HEATING, ThermMode.AWAY)
            elif mode == "FROST_GUARD":
                await c.async_set_home_data(home.id, TemperatureControlMode.HEATING, ThermMode.HG)
            elif mode == "HOT_WATER_ONLY":
                await c.async_set_home_data(home.id, TemperatureControlMode.COOLING)
            else:
                raise HTTPException(400, f"Unknown mode {mode}")
        elif a == "holiday":
            await c.async_set_home_data(home.id, TemperatureControlMode.HEATING, ThermMode.AWAY)
        elif a == "cancel_holiday":
            await c.async_set_home_data(home.id, TemperatureControlMode.HEATING, ThermMode.SCHEDULE)
        elif a == "dhw_temperature":
            await c.async_set_hot_water_temperature(device.id, math.ceil(float(p["temperature"])))
        elif a in ("dhw_boost", "dhw_cancel_boost"):
            await c.async_set_state_for_module(home.id, device.id, a == "dhw_boost")
        else:
            raise HTTPException(400, f"This action is not available on vSMART: {a}")


# --------------------------------------------------------------------------- #
#  Demo backend: simulated house, so the UI can be tried without an account   #
# --------------------------------------------------------------------------- #


class DemoBackend:
    def __init__(self, cfg: dict):
        self.raw: list[dict] = []
        self.room = 20.4
        self.zone = {
            "index": 0, "name": "Living room", "active": True, "humidity": 46.0,
            "mode": "TIME_CONTROLLED", "modes": ["TIME_CONTROLLED", "MANUAL", "OFF"],
            "special_function": "NONE", "setback_temperature": 17.0, "manual_setpoint": 21.0,
            "quick_veto_end": None, "veto_temperature": None, "holiday_start": None, "holiday_end": None,
        }
        self.dhw = {
            "index": 255, "temperature": 49.0, "setpoint": 50.0, "min": 35.0, "max": 65.0,
            "mode": "TIME_CONTROLLED", "modes": ["TIME_CONTROLLED", "MANUAL", "OFF"],
            "special_function": "REGULAR",
        }

    async def start(self):
        log.info("Running in DEMO mode with simulated data")

    async def stop(self):
        pass

    def _schedule(self) -> float | None:
        z = self.zone
        if z["mode"] == "OFF":
            return None
        if z["mode"] == "MANUAL":
            return z["manual_setpoint"]
        h = dt.datetime.now().hour
        return 21.0 if 6 <= h < 22 else z["setback_temperature"]

    async def fetch(self) -> list[dict]:
        z = self.zone
        if z["quick_veto_end"] and dt.datetime.fromisoformat(z["quick_veto_end"]) < dt.datetime.now().astimezone():
            z["quick_veto_end"] = None
        target = z["veto_temperature"] if z["quick_veto_end"] else self._schedule()
        if z["holiday_end"]:
            target = 10.0
        outdoor = 6 + 4 * math.sin(time.time() / 7200)
        heating = target is not None and self.room < target - 0.2
        self.room += (0.15 if heating else -0.05) + random.uniform(-0.03, 0.03)
        flow = 45 + random.uniform(-2, 2) if heating else 24 + random.uniform(-1, 1)
        self.dhw["temperature"] = max(40, min(self.dhw["setpoint"], self.dhw["temperature"] + random.uniform(-0.5, 0.6)))
        zone = {
            **z,
            "room_temperature": round(self.room, 1),
            "target": target,
            "schedule_setpoint": self._schedule(),
            "heating_state": "HEATING_UP" if heating else "IDLE",
            "circuit": {
                "index": 0, "state": "HEATING" if heating else "STANDBY",
                "flow_temperature": round(flow, 1), "flow_setpoint": 46.0 if heating else 0,
                "heating_curve": 1.2, "flow_min": 15, "flow_max": 75, "summer_limit": 21,
            },
        }
        summary = {
            "id": "demo-system", "name": "Demo house", "controller": "TLI", "connected": True,
            "firmware": "0357.40.31", "gateway": "VR921",
            "outdoor_temperature": round(outdoor, 1), "outdoor_temperature_24h": 6.2,
            "water_pressure": round(1.6 + random.uniform(-0.05, 0.05), 2),
            "system_flow_temperature": round(flow, 1),
            "cylinder_top": None, "cylinder_bottom": None, "energy_manager_state": None,
            "trouble_codes": [],
            "zones": [zone], "dhw": [dict(self.dhw)],
            "devices": [
                {"name": "ecoTEC plus", "product": "ecoTEC plus VC 25CS/1-5", "type": "BOILER",
                 "serial": "21223300100211110000000000N8", "article": "0010021111", "ebus_id": "BAI00",
                 "first_data": None, "last_data": None,
                 "operational_data": {"water_pressure": {"value": 1.6, "unit": "BAR"}},
                 "rts": None, "mpc": None, "trouble_codes": []},
                {"name": "sensoCOMFORT", "product": "sensoCOMFORT VRC 720f", "type": "CONTROL",
                 "serial": "21213300100279450000000000N2", "article": "0020279450", "ebus_id": "CTLV2",
                 "first_data": None, "last_data": None, "operational_data": {},
                 "rts": None, "mpc": None, "trouble_codes": []},
            ],
            "ambisense_rooms": [],
        }
        self.raw = [{"note": "demo mode - the real API returns far more fields here", **summary}]
        return [summary]

    async def energy(self) -> list[dict]:
        today = dt.date.today()
        days = [(today - dt.timedelta(days=13 - i)).isoformat() for i in range(14)]
        rnd = random.Random(1)
        out = []
        for mode, base in (("HEATING", 18000), ("DOMESTIC_HOT_WATER", 4000)):
            pts = [[d, round(base * rnd.uniform(0.6, 1.3))] for d in days]
            out.append({"device": "ecoTEC plus", "operation_mode": mode, "energy_type": "CONSUMED_PRIMARY_ENERGY",
                        "unit": "Wh", "total": sum(p[1] for p in pts), "points": pts})
        return out

    async def act(self, a: str, p: dict):
        z, d = self.zone, self.dhw
        if a == "zone_quick_veto":
            z["veto_temperature"] = float(p["temperature"])
            z["quick_veto_end"] = (dt.datetime.now().astimezone() + dt.timedelta(hours=float(p.get("hours") or 3))).isoformat()
            z["special_function"] = "QUICK_VETO"
        elif a == "zone_cancel_veto":
            z["quick_veto_end"] = None
            z["special_function"] = "NONE"
        elif a == "zone_mode":
            z["mode"] = p["mode"]
        elif a == "zone_manual_setpoint":
            z["manual_setpoint"] = float(p["temperature"])
        elif a == "zone_setback":
            z["setback_temperature"] = float(p["temperature"])
        elif a == "dhw_temperature":
            d["setpoint"] = float(p["temperature"])
        elif a == "dhw_mode":
            d["mode"] = p["mode"]
        elif a == "dhw_boost":
            d["special_function"] = "CYLINDER_BOOST"
        elif a == "dhw_cancel_boost":
            d["special_function"] = "REGULAR"
        elif a == "holiday":
            start = dt.datetime.now().astimezone()
            z["holiday_start"] = start.isoformat()
            z["holiday_end"] = (start + dt.timedelta(days=float(p.get("days") or 7))).isoformat()
            z["special_function"] = "HOLIDAY"
        elif a == "cancel_holiday":
            z["holiday_start"] = z["holiday_end"] = None
            z["special_function"] = "NONE"
        else:
            raise HTTPException(400, f"Unknown action {a}")


# --------------------------------------------------------------------------- #
#  External (independent) room sensor                                         #
# --------------------------------------------------------------------------- #


def dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if data is None:
            return None
        data = data[int(part)] if isinstance(data, list) else data.get(part)
    return data


class ExternalSensor:
    def __init__(self, cfg: dict, history: History):
        self.cfg = cfg
        self.history = history
        self.temperature: float | None = None
        self.humidity: float | None = None
        self.updated: float | None = None
        self.error: str | None = None
        self.on_event = None

    @property
    def enabled(self) -> bool:
        return self.cfg["mode"] != "none"

    @property
    def fresh(self) -> bool:
        return (
            self.temperature is not None
            and self.updated is not None
            and now_ts() - self.updated < self.cfg["max_age_minutes"] * 60
        )

    def record(self, temperature: float | None, humidity: float | None = None):
        if temperature is None:
            return
        self.temperature = float(temperature)
        self.humidity = None if humidity is None else float(humidity)
        self.updated = now_ts()
        self.error = None
        self.history.add({"external.temperature": self.temperature, "external.humidity": self.humidity})

    async def _read_shelly(self, session: aiohttp.ClientSession) -> tuple[float | None, float | None]:
        base = self.cfg["url"].rstrip("/")
        # Gen2/Gen3/Plus devices
        try:
            async with session.get(f"{base}/rpc/Shelly.GetStatus") as r:
                if r.status == 200:
                    st = await r.json(content_type=None)
                    t = dig(st, "temperature:0.tC") or dig(st, "temperature:100.tC")
                    h = dig(st, "humidity:0.rh") or dig(st, "humidity:100.rh")
                    if t is not None:
                        return t, h
        except aiohttp.ClientError:
            pass
        # Gen1 devices
        async with session.get(f"{base}/status") as r:
            st = await r.json(content_type=None)
        t = dig(st, "tmp.tC") or dig(st, "ext_temperature.0.tC")
        h = dig(st, "hum.value") or dig(st, "ext_humidity.0.hum")
        return t, h

    async def _read_http_json(self, session: aiohttp.ClientSession) -> tuple[float | None, float | None]:
        async with session.get(self.cfg["url"]) as r:
            data = await r.json(content_type=None)
        return dig(data, self.cfg["temperature_path"]), dig(data, self.cfg["humidity_path"] or "")

    async def run(self):
        if self.cfg["mode"] not in ("shelly", "http_json"):
            return  # "push" mode receives readings over HTTP instead
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    reader = self._read_shelly if self.cfg["mode"] == "shelly" else self._read_http_json
                    t, h = await reader(session)
                    if t is None:
                        self._set_error("No temperature found in sensor response")
                    else:
                        if self.error and self.on_event:
                            self.on_event("sensor", "app", f"External sensor back: {t} °C")
                        self.record(t, h)
                except Exception as e:
                    self._set_error(f"{type(e).__name__}: {e}")
                await asyncio.sleep(self.cfg["poll_seconds"])

    def _set_error(self, msg: str):
        if msg != self.error and self.on_event:
            self.on_event("sensor", "app", f"External sensor read failed: {msg}")
        self.error = msg
        log.warning("External sensor read failed: %s", msg)

    def summary(self) -> dict:
        return {
            "enabled": self.enabled,
            "mode": self.cfg["mode"],
            "name": self.cfg["name"],
            "temperature": self.temperature,
            "humidity": self.humidity,
            "updated": self.updated,
            "fresh": self.fresh,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
#  Hub: polling loop, history, compensation                                   #
# --------------------------------------------------------------------------- #


class Hub:
    def __init__(self, cfg: dict, backend, data_dir: Path):
        self.cfg = cfg
        self.backend = backend
        self.history = History(data_dir / "history.sqlite")
        self.sensor = ExternalSensor(cfg["external_sensor"], self.history)
        self.sensor.on_event = self.event
        self.settings_path = data_dir / "settings.json"
        self.settings = {"compensation_enabled": False, "compensation_system": None, "compensation_zone": None}
        if self.settings_path.exists():
            self.settings.update(json.loads(self.settings_path.read_text()))
        self.systems: list[dict] = []
        self.energy: list[dict] = []
        self.energy_updated = 0.0
        self.updated: float | None = None
        self.error: str | None = None
        self.compensation_status: dict = {}
        self.comp_last_change = 0.0
        self.comp_last_target: float | None = None
        self.comp_last_reason: str | None = None
        self.comp_last_own = 0.0
        self.comp_target_before: float | None = None
        self._previous: dict[str, Any] = {}
        self._recent_commands: list[tuple[float, str, dict]] = []
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def event(self, kind: str, source: str, message: str, data: dict | None = None):
        """
        kind:   check (algorithm looked, nothing to do) | command (sent to the thermostat) |
                detected (a change seen in the thermostat data) | sensor | error | system
        source: algorithm | you | thermostat | app
        """
        log.info("[%s/%s] %s", kind, source, message)
        try:
            self.history.add_event(kind, source, message, data)
        except Exception:
            log.exception("Could not store event")

    async def command(self, source: str, action: str, params: dict, why: str):
        """Send a change to the thermostat and log who asked for it and why."""
        try:
            await self.backend.act(action, params)
        except Exception as e:
            self.event("error", source, f"FAILED: {why} ({type(e).__name__}: {e})", {"action": action, **params})
            raise
        self._recent_commands.append((now_ts(), action, params))
        self.event("command", source, why, {"action": action, **params})

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, indent=2))

    async def start(self):
        self._tasks = [asyncio.create_task(self._poll_loop()), asyncio.create_task(self.sensor.run())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await self.backend.stop()

    def refresh_soon(self):
        self._wake.set()

    async def _poll_loop(self):
        started = False
        while True:
            try:
                if not started:
                    await self.backend.start()
                    started = True
                    self.event("system", "app", f"Connected ({type(self.backend).__name__.replace('Backend', '')})")
                self.systems = await self.backend.fetch()
                self.updated = now_ts()
                if self.error:
                    self.event("system", "app", "Connection restored")
                self.error = None
                self._detect_changes()
                self._record_history()
                await self._compensate()
                if now_ts() - self.energy_updated > self.cfg["vaillant"]["energy_refresh_minutes"] * 60:
                    try:
                        self.energy = await self.backend.energy()
                    except Exception as e:
                        log.warning("Energy data unavailable: %s", e)
                    self.energy_updated = now_ts()
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self.error:
                    self.event("error", "app", f"Update failed: {msg}")
                self.error = msg
                log.exception("Update failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.cfg["vaillant"]["poll_seconds"])
                await asyncio.sleep(4)  # give the cloud a moment to apply a change
            except asyncio.TimeoutError:
                pass

    def _record_history(self):
        values: dict[str, float | None] = {}
        for i, s in enumerate(self.systems):
            p = f"s{i}." if len(self.systems) > 1 else ""
            values[p + "outdoor"] = s["outdoor_temperature"]
            values[p + "water_pressure"] = s["water_pressure"]
            values[p + "flow"] = s["system_flow_temperature"]
            for z in s["zones"]:
                values[f"{p}zone{z['index']}.room"] = z["room_temperature"]
                values[f"{p}zone{z['index']}.target"] = z["target"]
                values[f"{p}zone{z['index']}.humidity"] = z["humidity"]
                values[f"{p}zone{z['index']}.wanted"] = z["schedule_setpoint"]
                if z["circuit"]:
                    values[f"{p}zone{z['index']}.flow"] = z["circuit"]["flow_temperature"]
            for d in s["dhw"]:
                values[f"{p}dhw{d['index']}"] = d["temperature"]
        self.history.add(values)

    WATCHED = {
        "target": "target temperature",
        "mode": "mode",
        "special_function": "special function",
        "quick_veto_end": "temporary override end",
        "schedule_setpoint": "schedule temperature",
        "setback_temperature": "night temperature",
        "manual_setpoint": "manual temperature",
        "setpoint": "hot water temperature",
    }

    def _caused_by_app(self, group: str, field: str, new: Any) -> bool:
        """Was this change most likely the result of a command this app sent in the last 15 minutes?"""
        for _, action, p in self._recent_commands:
            if group == "zone" and (action.startswith("zone_") or action in ("holiday", "cancel_holiday")):
                if field == "target" and action == "zone_quick_veto":
                    if new is not None and abs(float(new) - float(p["temperature"])) < 0.05:
                        return True
                    continue
                return True
            if group == "dhw" and action.startswith("dhw_"):
                if field == "setpoint" and action == "dhw_temperature":
                    if new is not None and abs(float(new) - float(p["temperature"])) < 0.5:
                        return True
                    continue
                return True
        return False

    def _detect_changes(self):
        """Log every setting that changed since the last poll, and work out who changed it."""
        current: dict[str, Any] = {}
        for s in self.systems:
            for z in s["zones"]:
                for k in ("target", "mode", "special_function", "quick_veto_end", "schedule_setpoint",
                          "setback_temperature", "manual_setpoint"):
                    current[f"zone|{z['name']}|{k}"] = z.get(k)
            for d in s["dhw"]:
                for k in ("setpoint", "special_function", "mode"):
                    current[f"dhw|Hot water|{k}"] = d.get(k)
        self._recent_commands = [c for c in self._recent_commands if now_ts() - c[0] < 15 * 60]
        if self._previous:
            changed = [(k, self._previous.get(k), v) for k, v in current.items()
                       if k in self._previous and self._previous[k] != v]
            # decide per zone / hot water: if the target moved to something the app didn't ask for,
            # the whole group of changes in this poll came from outside
            outside = set()
            for key, old, new in changed:
                group, name, field = key.split("|")
                if field in ("target", "setpoint") and not self._caused_by_app(group, field, new):
                    outside.add(name)
            for key, old, new in changed:
                group, name, field = key.split("|")
                if field == "quick_veto_end" and old and new and name not in outside:
                    continue  # the app renewing its own override is already logged as a command
                if field == "schedule_setpoint":
                    who, note = "thermostat", "schedule moved to the next time slot"
                elif name not in outside and self._caused_by_app(group, field, new):
                    who, note = "app", "result of a command sent from this app"
                else:
                    who, note = "thermostat", "changed on the thermostat, in the phone app, or by its schedule"
                fmt_v = lambda v: "–" if v is None else (time.strftime("%a %H:%M", time.localtime(
                    dt.datetime.fromisoformat(v).timestamp())) if field == "quick_veto_end" else v)
                self.event("detected", who,
                           f"{name}: {self.WATCHED.get(field, field)} {fmt_v(old)} → {fmt_v(new)} ({note})",
                           {"field": field, "old": old, "new": new})
        self._previous = current

    def _comp_zone(self) -> tuple[dict | None, dict | None]:
        for s in self.systems:
            if self.settings["compensation_system"] in (None, s["id"]):
                for z in s["zones"]:
                    if self.settings["compensation_zone"] in (None, z["index"]):
                        return s, z
        return None, None

    def _comp_note(self, reason: str, data: dict | None = None, kind: str = "check"):
        """Log an algorithm decision, but only when the reasoning changes (avoids one line per poll)."""
        self.compensation_status["reason"] = reason
        if reason != self.comp_last_reason:
            self.comp_last_reason = reason
            self.event(kind, "algorithm", reason, data)

    async def _compensate(self):
        """
        Make the boiler aim for the *external* sensor's temperature.

        The Vaillant controller can only regulate on its own sensor, so we shift
        its setpoint by the difference between the two sensors, using a
        temporary "quick veto" which expires by itself if this app stops.
        """
        st = self.compensation_status = {"enabled": self.settings["compensation_enabled"], "state": "off"}
        if not st["enabled"]:
            self.comp_last_reason = None
            return
        s, z = self._comp_zone()
        if z is None:
            st["state"] = "no zone"
            return self._comp_note("Paused: no heating zone found")
        if not self.sensor.fresh:
            st["state"] = "waiting for a fresh external reading"
            age = f"last reading {ago_text(self.sensor.updated)}" if self.sensor.updated else "no reading yet"
            return self._comp_note(f"Paused: external sensor has no recent reading ({age}). "
                                   "The thermostat keeps its current setting; any override expires by itself.")
        if z["room_temperature"] is None:
            st["state"] = "the Vaillant controller does not report a room temperature"
            return self._comp_note("Paused: the thermostat does not report a room temperature")
        if z["holiday_end"] or z["special_function"] == "HOLIDAY" or z["schedule_setpoint"] is None:
            st["state"] = "paused (zone off, away or holiday)"
            return self._comp_note(f"Paused: zone is in mode {z['mode']} / {z['special_function']}, nothing to correct")
        c = self.cfg["compensation"]
        wanted = z["schedule_setpoint"]
        thermo, ext = z["room_temperature"], self.sensor.temperature
        diff = thermo - ext
        offset = max(-c["max_offset"], min(c["max_offset"], diff))
        target = round_half(wanted + offset)
        st.update(state="active", wanted=wanted, offset=round(offset, 2), setpoint=target, zone=z["name"])

        veto_left = None
        if z["quick_veto_end"]:
            veto_left = (dt.datetime.fromisoformat(z["quick_veto_end"]) - dt.datetime.now().astimezone()).total_seconds()
        since_change = now_ts() - self.comp_last_change
        too_soon = since_change < c["min_minutes_between_changes"] * 60
        expiring = veto_left is not None and veto_left < 20 * 60

        same = lambda a, b: a is not None and b is not None and abs(a - b) < 0.05
        ours = self.comp_last_target is not None and (
            same(z["target"], self.comp_last_target)
            # right after a command the cloud may still show the value from before it
            or (now_ts() - self.comp_last_own < 10 * 60 and same(z["target"], self.comp_target_before))
        )
        if veto_left is not None and not ours:
            self.comp_last_target = None
            end = dt.datetime.fromisoformat(z["quick_veto_end"]).astimezone().strftime("%a %H:%M")
            st["state"] = f"stepping back until {end}"
            return self._comp_note(
                f"Someone set a temporary {z['target']} °C (until {end}) on the thermostat, phone app or dashboard. "
                f"The algorithm respects it and takes over again when it ends", {"target": z["target"], "until": end})

        warmer = "warmer" if diff > 0 else "colder"
        calc = (f"Thermostat {thermo:.1f} °C, sensor {ext:.1f} °C → thermostat reads {abs(diff):.1f} °C {warmer}"
                + (f" (limited to ±{c['max_offset']} °C)" if offset != diff else "")
                + f". Schedule wants {wanted:.1f} °C, so the thermostat should aim for "
                f"{wanted:.1f} {'+' if offset >= 0 else '−'} {abs(offset):.1f} = {wanted + offset:.2f} → {target:.1f} °C")
        data = {"thermostat": thermo, "sensor": ext, "difference": round(diff, 2), "offset": round(offset, 2),
                "wanted": wanted, "setpoint": target, "override_left_min": None if veto_left is None else round(veto_left / 60)}
        wait = f"waiting {round((c['min_minutes_between_changes'] * 60 - since_change) / 60)} min more (min. {c['min_minutes_between_changes']} min between changes)"

        if target == wanted:
            if veto_left and self.comp_last_target is not None:
                if too_soon:
                    return self._comp_note(f"{calc}. Sensors agree: will cancel the override, {wait}", data)
                await self.command("algorithm", "zone_cancel_veto", {"system": s["id"], "zone": z["index"]},
                                   f"{calc}. Sensors agree, so the override is cancelled and the normal schedule applies")
                self.comp_last_target = None
                self.comp_last_change = now_ts()
                self.comp_last_reason = None
                st["last_action"] = "sensors agree, cancelled override"
                return
            return self._comp_note(f"{calc}. No correction needed", data)

        if target == self.comp_last_target and veto_left is not None and not expiring:
            return self._comp_note(f"{calc}. Already set, override still valid for {round(veto_left / 60)} min: nothing to do", data)
        if too_soon:
            return self._comp_note(f"{calc}. Change needed, but {wait}", data)
        why = ("override about to expire, renewing it" if target == self.comp_last_target and expiring
               else "no override active, setting one" if veto_left is None
               else f"changing override from {self.comp_last_target if self.comp_last_target is not None else z['target']} °C")
        await self.command(
            "algorithm", "zone_quick_veto",
            {"system": s["id"], "zone": z["index"], "temperature": target, "hours": c["veto_hours"]},
            f"{calc}. {why[0].upper() + why[1:]}: set {target:.1f} °C for {c['veto_hours']} h",
        )
        self.comp_target_before = z["target"]
        self.comp_last_target = target
        self.comp_last_change = self.comp_last_own = now_ts()
        self.comp_last_reason = None
        st["last_action"] = f"set {target} °C on the Vaillant controller"
        self.refresh_soon()

    def state(self) -> dict:
        return {
            "updated": self.updated,
            "error": self.error,
            "demo": isinstance(self.backend, DemoBackend),
            "poll_seconds": self.cfg["vaillant"]["poll_seconds"],
            "systems": self.systems,
            "external": self.sensor.summary(),
            "compensation": {**self.compensation_status, **self.settings, "config": self.cfg["compensation"]},
            "energy": self.energy,
        }


# --------------------------------------------------------------------------- #
#  Web app                                                                    #
# --------------------------------------------------------------------------- #


def describe_action(name: str, p: dict) -> str:
    t = p.get("temperature")
    return {
        "zone_quick_veto": f"Temporary temperature {t} °C for {p.get('hours')} h",
        "zone_cancel_veto": "Cancelled temporary temperature",
        "zone_mode": f"Mode set to {p.get('mode')}",
        "zone_manual_setpoint": f"Manual/day temperature set to {t} °C",
        "zone_setback": f"Night/setback temperature set to {t} °C",
        "dhw_temperature": f"Hot water temperature set to {t} °C",
        "dhw_mode": f"Hot water mode set to {p.get('mode')}",
        "dhw_boost": "Hot water boost started",
        "dhw_cancel_boost": "Hot water boost stopped",
        "holiday": f"Away mode for {p.get('days')} days",
        "cancel_holiday": "Away mode ended",
    }.get(name, name)


def create_app(hub: Hub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        yield
        await hub.stop()

    app = FastAPI(title="Vaillant Dashboard", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/")
    async def index():
        return FileResponse(HERE / "static" / "index.html")

    @app.get("/api/state")
    async def state():
        return hub.state()

    @app.get("/api/raw")
    async def raw():
        return JSONResponse(json.loads(json.dumps(hub.backend.raw, default=str)))

    @app.get("/api/history")
    async def history(hours: float = 24):
        return hub.history.query(hours)

    @app.get("/api/history.csv")
    async def history_csv(hours: float = 24 * 30):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            hub.history.export_csv(hours),
            headers={"Content-Disposition": "attachment; filename=heating-history.csv"},
        )

    @app.get("/api/events")
    async def events(hours: float = 72, kinds: str = "", limit: int = 500):
        return hub.history.events(hours, [k for k in kinds.split(",") if k] or None, limit)

    @app.get("/api/events.csv")
    async def events_csv(hours: float = 24 * 30):
        import csv, io
        from fastapi.responses import PlainTextResponse

        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["time", "kind", "source", "message", "details"])
        for e in reversed(hub.history.events(hours, None, 100000)):
            w.writerow([dt.datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds"), e["kind"], e["source"],
                        e["message"], json.dumps(e["data"]) if e["data"] else ""])
        return PlainTextResponse(out.getvalue(), headers={"Content-Disposition": "attachment; filename=heating-log.csv"})

    @app.post("/api/refresh")
    async def refresh():
        hub.refresh_soon()
        return {"ok": True}

    @app.post("/api/action")
    async def action(request: Request):
        body = await request.json()
        name = body.pop("action", None)
        if not hub.systems:
            raise HTTPException(503, "No data from Vaillant yet")
        try:
            await hub.command("you", name, body, describe_action(name, body))
        except HTTPException:
            raise
        except Exception as e:
            log.exception("Action %s failed", name)
            raise HTTPException(502, f"{type(e).__name__}: {e}")
        if name.startswith("zone_") and hub.settings["compensation_enabled"]:
            # a manual change wins over the automatic compensation
            hub.comp_last_change = now_ts()
        hub.refresh_soon()
        return {"ok": True}

    @app.post("/api/compensation")
    async def compensation(request: Request):
        body = await request.json()
        for k in ("compensation_enabled", "compensation_system", "compensation_zone"):
            if k in body:
                hub.settings[k] = body[k]
        hub.save_settings()
        hub.event("system", "you", "Heat to the external sensor: " + ("ON" if hub.settings["compensation_enabled"] else "OFF"))
        if not hub.settings["compensation_enabled"] and hub.comp_last_target is not None:
            s, z = hub._comp_zone()
            if z and z["quick_veto_end"]:
                await hub.command("algorithm", "zone_cancel_veto", {"system": s["id"], "zone": z["index"]},
                                  "Switched off: removing the override, back to the normal schedule")
            hub.comp_last_target = None
        hub.comp_last_change = 0
        hub.refresh_soon()
        return {"ok": True}

    # Push endpoint for sensors that send their readings (e.g. Shelly H&T "actions"/webhooks):
    #   http://<this-pc>:8321/api/external?temp=21.3&hum=45
    @app.api_route("/api/external", methods=["GET", "POST"])
    async def external(request: Request):
        q = dict(request.query_params)
        if request.method == "POST" and not q:
            try:
                q = await request.json()
            except Exception:
                q = dict(await request.form())
        t = q.get("temp", q.get("temperature", q.get("tC")))
        h = q.get("hum", q.get("humidity", q.get("rh")))
        if t is None:
            raise HTTPException(400, "Send temp=<°C> (and optionally hum=<%>)")
        hub.sensor.record(float(t), None if h in (None, "") else float(h))
        return {"ok": True}

    return app


def main():
    parser = argparse.ArgumentParser(description="Local dashboard for a Vaillant (myVAILLANT) heating system")
    parser.add_argument("--config", default=str(HERE / "config.toml"))
    parser.add_argument("--demo", action="store_true", help="simulated data, no Vaillant account needed")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(Path(args.config))
    if not args.demo and not Path(args.config).exists():
        raise SystemExit(
            f"No config found at {args.config}. Copy config.example.toml to config.toml "
            "and fill in your myVAILLANT login, or run with --demo."
        )
    data_dir = HERE / ("data-demo" if args.demo else "data")
    data_dir.mkdir(exist_ok=True)
    from logging.handlers import RotatingFileHandler

    fh = RotatingFileHandler(data_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    if args.demo:
        backend = DemoBackend(cfg)
    elif cfg["system"] == "vsmart":
        backend = VsmartBackend(cfg["vsmart"], data_dir)
        cfg["vaillant"]["poll_seconds"] = max(cfg["vaillant"]["poll_seconds"], 300)  # Netatmo rate limits
    else:
        backend = VaillantBackend(cfg["vaillant"])
    if args.demo:
        cfg["vaillant"]["poll_seconds"] = 10
    hub = Hub(cfg, backend, data_dir)

    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    if cfg["server"]["open_browser"] and not args.no_browser:
        import threading

        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(create_app(hub), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
