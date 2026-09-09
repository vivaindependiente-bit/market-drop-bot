#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

BASE = "https://api.oddspapi.io/v4"
TG_BASE = "https://api.telegram.org"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
CACHE_PATH = HERE / "cache.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "bot.log"

DEFAULT_CONFIG = {
    "bookmakers": ["bet365.bet.ar", "betano"],
    "watch_tournament_patterns": [
        "uefa champions league",
        "premier league",
        "championship",
        "laliga",
        "serie a",
        "bundesliga",
        "ligue 1",
        "liga profesional",
        "brasileirao",
        "copa libertadores",
        "copa sudamericana",
        "primeira liga",
        "eredivisie"
    ],
    "fixture_horizon_hours": 336,
    "fixture_refresh_hours": 12,
    "metadata_refresh_days": 30,
    "quota_reserve_requests": 70,
    "quota_check_hours": 1,
    "historical_request_spacing_seconds": 6.5,
    "timezone": "America/Argentina/Buenos_Aires",
    "alert_on_first_seen": False,
    "targets": ["shots", "shots_on_target", "fouls_committed", "tackles"],
    "request_timeout_seconds": 30
}

TARGET_LABELS = {
    "shots": "🎯 Remates",
    "shots_on_target": "🥅 Remates al arco",
    "fouls_committed": "🚫 Faltas cometidas",
    "tackles": "🛡️ Entradas / tackles",
}

def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"No pude leer {path.name}: {e}")
    return default

def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def require_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {}) if CONFIG_PATH.exists() else {}
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)

    env_map = {
        "oddspapi_key": "ODDSPAPI_KEY",
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_chat_id": "TELEGRAM_CHAT_ID",
    }
    for key, env_name in env_map.items():
        if os.getenv(env_name):
            cfg[key] = os.environ[env_name]

    if os.getenv("BOOKMAKERS"):
        cfg["bookmakers"] = [x.strip() for x in os.environ["BOOKMAKERS"].split(",") if x.strip()]
    if os.getenv("TARGETS"):
        cfg["targets"] = [x.strip() for x in os.environ["TARGETS"].split(",") if x.strip()]
    if os.getenv("TIMEZONE"):
        cfg["timezone"] = os.environ["TIMEZONE"]
    if os.getenv("HISTORICAL_REQUEST_SPACING_SECONDS"):
        cfg["historical_request_spacing_seconds"] = float(os.environ["HISTORICAL_REQUEST_SPACING_SECONDS"])

    missing = [k for k in ("oddspapi_key", "telegram_bot_token", "telegram_chat_id") if not cfg.get(k)]
    if missing:
        print("Faltan variables de entorno/configuración: " + ", ".join(missing))
        raise SystemExit(1)
    return cfg

def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()

class OddsPapi:
    def __init__(self, api_key: str, timeout: int = 30):
        self.api_key = api_key
        self.timeout = timeout
        self.s = requests.Session()

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        params["apiKey"] = self.api_key

        # OddsPapi devuelve retryMs/retryAfter en los 429 de rate limit.
        # Reintentar automáticamente evita perder un mercado por unas décimas.
        for attempt in range(4):
            r = self.s.get(BASE + endpoint, params=params, timeout=self.timeout)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()

            try:
                payload = r.json()
            except Exception:
                payload = {}
            err = payload.get("error", payload) if isinstance(payload, dict) else {}
            code = str(err.get("code") or "HTTP_429")
            details = str(err.get("details") or err.get("message") or "")
            retry_ms = err.get("retryMs")

            if code == "RATE_LIMITED" and attempt < 3:
                try:
                    wait = max(float(retry_ms) / 1000.0, 1.0) if retry_ms is not None else 6.0
                except Exception:
                    wait = 6.0
                log(f"Rate limit OddsPapi en {endpoint}; reintento en {wait + 0.75:.2f}s")
                time.sleep(wait + 0.75)
                continue

            raise RuntimeError(f"OddsPapi 429 [{code}] {details}".strip())

        raise RuntimeError("OddsPapi 429 persistente")

    def account(self) -> Any:
        return self.get("/account")

    def markets(self) -> Any:
        return self.get("/markets", {"language": "en"})

    def tournaments(self) -> Any:
        return self.get("/tournaments", {"sportId": 10, "language": "en"})

    def participants(self) -> Any:
        return self.get("/participants", {"sportId": 10, "language": "en"})

    def fixtures_range(self, start: datetime, end: datetime, bookmakers: List[str]) -> Any:
        # OddsPapi exige rangos menores a 10 días cuando se consulta por sportId.
        if end <= start or end - start >= timedelta(days=10):
            raise ValueError("El rango de /fixtures debe ser mayor a 0 y menor a 10 días")
        return self.get("/fixtures", {
            "sportId": 10,
            "from": start.isoformat().replace("+00:00", "Z"),
            "to": end.isoformat().replace("+00:00", "Z"),
            "statusId": 0,
            "hasOdds": "true",
            "bookmakers": ",".join(bookmakers),
            "language": "en",
        })

    def historical(self, fixture_id: str, bookmakers: List[str]) -> Any:
        return self.get("/historical-odds", {
            "fixtureId": fixture_id,
            "bookmakers": ",".join(bookmakers[:3]),
        })

class Telegram:
    def __init__(self, token: str, chat_id: str, timeout: int = 30):
        self.token = token
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.s = requests.Session()

    def send(self, text: str) -> None:
        chat_ids = [self.chat_id]
        group_chat_id = os.getenv("TELEGRAM_GROUP_CHAT_ID", "").strip()
        if group_chat_id and group_chat_id not in chat_ids:
            chat_ids.append(group_chat_id)

        sent = 0
        errors: List[str] = []
        for chat_id in chat_ids:
            try:
                r = self.s.post(
                    f"{TG_BASE}/bot{self.token}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                    timeout=self.timeout,
                )
                r.raise_for_status()
                sent += 1
            except Exception as e:
                errors.append(f"{chat_id}: {e}")
                log(f"No pude enviar Telegram a {chat_id}: {e}")
        if sent == 0:
            raise RuntimeError("No se pudo enviar Telegram: " + "; ".join(errors))

def classify_market(m: Dict[str, Any]) -> Optional[str]:
    if not m.get("playerProp"):
        return None
    name = normalize(m.get("marketName", ""))
    mtype = normalize(m.get("marketType", ""))

    if any(x in name for x in ("shots on target", "shots on goal")) or \
       any(x in mtype for x in ("shots-on-target", "shots-on-goal")):
        return "shots_on_target"

    if "shot" in name or "shot" in mtype:
        return "shots"

    if "tackle" in name or "tackle" in mtype:
        return "tackles"

    if ("foul" in name and any(x in name for x in ("committed", "commit"))) or \
       ("foul" in mtype and any(x in mtype for x in ("committed", "commit"))):
        return "fouls_committed"

    if name.startswith("player foul") and "fouled" not in name:
        return "fouls_committed"

    return None

def build_market_catalog(markets: List[Dict[str, Any]], targets: Set[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    id_to_category: Dict[str, str] = {}
    id_to_name: Dict[str, str] = {}
    for m in markets or []:
        cat = classify_market(m)
        if cat and cat in targets:
            mid = str(m.get("marketId"))
            id_to_category[mid] = cat
            id_to_name[mid] = m.get("marketName") or mid
    return id_to_category, id_to_name

def as_fixture_list(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict) and x.get("fixtureId")]
    if isinstance(data, dict):
        if data.get("fixtureId"):
            return [data]
        for key in ("fixtures", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict) and x.get("fixtureId")]
    return []

def latest_active(player_history: Any) -> bool:
    if isinstance(player_history, dict):
        return bool(player_history.get("active"))
    if not isinstance(player_history, list) or not player_history:
        return False
    items = [x for x in player_history if isinstance(x, dict)]
    if not items:
        return False

    def stamp(x: Dict[str, Any]) -> str:
        return str(x.get("createdAt") or x.get("changedAt") or "")

    items.sort(key=stamp, reverse=True)
    return bool(items[0].get("active"))

def market_has_active_players(market_data: Dict[str, Any]) -> bool:
    if market_data.get("marketActive") is False:
        return False
    outcomes = market_data.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return False
    for outcome in outcomes.values():
        if not isinstance(outcome, dict):
            continue
        players = outcome.get("players") or {}
        if not isinstance(players, dict):
            continue
        for history in players.values():
            if latest_active(history):
                return True
    return False

def extract_active_categories(
    hist: Dict[str, Any],
    id_to_category: Dict[str, str],
    bookmakers: List[str],
) -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {b: set() for b in bookmakers}
    books = hist.get("bookmakers") or hist.get("bookmakerOdds") or {}
    if not isinstance(books, dict):
        return out

    for book in bookmakers:
        bdata = books.get(book) or {}
        markets = bdata.get("markets") or {}
        if not isinstance(markets, dict):
            continue
        for mid, mdata in markets.items():
            cat = id_to_category.get(str(mid))
            if not cat or not isinstance(mdata, dict):
                continue
            if market_has_active_players(mdata):
                out[book].add(cat)
    return out

def select_tournaments(
    tournaments: List[Dict[str, Any]], patterns: List[str]
) -> Tuple[Set[int], Dict[str, str]]:
    wanted = [normalize(x) for x in patterns if str(x).strip()]
    selected: Set[int] = set()
    names: Dict[str, str] = {}
    for t in tournaments or []:
        tid = t.get("tournamentId")
        if tid is None:
            continue
        haystack = " ".join([
            normalize(t.get("tournamentName", "")),
            normalize(t.get("tournamentSlug", "")),
            normalize(t.get("categoryName", "")),
        ])
        if not wanted or any(p in haystack for p in wanted):
            try:
                selected.add(int(tid))
                names[str(tid)] = t.get("tournamentName") or f"Torneo {tid}"
            except Exception:
                pass
    return selected, names

def fixture_name(f: Dict[str, Any], participants: Dict[str, str]) -> str:
    p1 = str(f.get("participant1Id", ""))
    p2 = str(f.get("participant2Id", ""))
    n1 = participants.get(p1, f"Equipo {p1}")
    n2 = participants.get(p2, f"Equipo {p2}")
    return f"{n1} - {n2}"

def fmt_local_time(iso: Optional[str], tz_name: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ZoneInfo:
            dt = dt.astimezone(ZoneInfo(tz_name))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return str(iso)

def bookmaker_label(slug: str) -> str:
    if slug == "bet365.bet.ar":
        return "BET365 AR"
    if slug.startswith("bet365"):
        return "BET365"
    if slug.startswith("betano"):
        return "BETANO"
    return slug.upper()

def participant_name_map(data: Any) -> Dict[str, str]:
    if isinstance(data, dict):
        # Si ya viene como {id: nombre}, conservarlo.
        if all(not isinstance(v, dict) for v in data.values()):
            return {str(k): str(v) for k, v in data.items()}
        items = data.get("participants") or data.get("data") or data.get("results") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    out: Dict[str, str] = {}
    for p in items:
        if not isinstance(p, dict):
            continue
        pid = p.get("participantId") or p.get("id")
        name = p.get("participantName") or p.get("name") or p.get("shortName")
        if pid is not None and name:
            out[str(pid)] = str(name)
    return out

def quota_info(account: Any) -> Tuple[int, int]:
    if not isinstance(account, dict):
        return 0, 0
    subs = account.get("subscriptions") or []
    if isinstance(subs, dict):
        subs = [subs]
    if not isinstance(subs, list):
        subs = []
    sub = next((x for x in subs if isinstance(x, dict) and x.get("is_active")), None)
    if sub is None:
        sub = next((x for x in subs if isinstance(x, dict)), {})
    try:
        used = int(sub.get("request_count") or account.get("request_count") or 0)
    except Exception:
        used = 0
    try:
        limit = int(sub.get("request_limit") or account.get("request_limit") or 0)
    except Exception:
        limit = 0
    return used, limit

def quota_soft_stop(limit: int, reserve: int) -> int:
    if limit <= 0:
        return 0
    return max(0, limit - max(0, reserve))

def can_spend_quota(used: int, limit: int, cost: int, reserve: int) -> bool:
    if limit <= 0:
        return False
    return used + max(0, cost) <= quota_soft_stop(limit, reserve)

def fixture_request_cost(horizon_hours: int) -> int:
    # Cada llamada cubre como máximo 9 días (216 h), por debajo del límite de 10 días.
    return max(1, (max(1, horizon_hours) + 215) // 216)

def metadata_stale(cache: Dict[str, Any], days: int) -> bool:
    last = cache.get("metadata_updated_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        return datetime.now(timezone.utc) - dt > timedelta(days=days)
    except Exception:
        return True

def refresh_metadata(api: OddsPapi, cache: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    log("Actualizando catálogo de mercados, torneos y participantes...")
    markets = api.markets()
    time.sleep(1.1)
    tournaments = api.tournaments()
    time.sleep(1.1)
    participants = api.participants()

    id_to_category, id_to_name = build_market_catalog(markets, set(cfg["targets"]))
    selected_ids, tournament_names = select_tournaments(tournaments, cfg["watch_tournament_patterns"])

    cache.update({
        "metadata_updated_at": datetime.now(timezone.utc).isoformat(),
        "market_id_to_category": id_to_category,
        "market_id_to_name": id_to_name,
        "selected_tournament_ids": sorted(selected_ids),
        "tournament_names": tournament_names,
        "participants": participant_name_map(participants),
    })
    save_json(CACHE_PATH, cache)
    log(f"Mercados player-prop mapeados: {len(id_to_category)}")
    log(f"Torneos vigilados: {len(selected_ids)}")
    return cache

def discover_fixtures(api: OddsPapi, cache: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    horizon = int(cfg["fixture_horizon_hours"])
    log(f"Actualizando lista de partidos para próximas {horizon} h...")
    start = datetime.now(timezone.utc)
    end = start + timedelta(hours=horizon)
    cursor = start
    fixtures_by_id: Dict[str, Dict[str, Any]] = {}

    # Dos llamadas alcanzan para 14 días: 9 días + 5 días.
    while cursor < end:
        chunk_end = min(cursor + timedelta(hours=216), end)
        data = api.fixtures_range(cursor, chunk_end, cfg["bookmakers"])
        for f in as_fixture_list(data):
            fid = str(f.get("fixtureId") or "")
            if fid:
                fixtures_by_id[fid] = f
        cursor = chunk_end
        if cursor < end:
            time.sleep(1.1)

    fixtures = list(fixtures_by_id.values())
    selected_ids = set(int(x) for x in cache.get("selected_tournament_ids", []))
    if selected_ids:
        fixtures = [f for f in fixtures if int(f.get("tournamentId", -1)) in selected_ids]
    fixtures.sort(key=lambda x: str(x.get("startTime") or ""), reverse=True)

    cache["fixtures"] = fixtures
    cache["fixtures_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(CACHE_PATH, cache)
    log(f"Partidos activos en vigilancia: {len(fixtures)}")
    return cache

def fixture_cache_stale(cache: Dict[str, Any], hours: float) -> bool:
    last = cache.get("fixtures_updated_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        return datetime.now(timezone.utc) - dt > timedelta(hours=hours)
    except Exception:
        return True

def state_key(fixture_id: str, bookmaker: str, category: str) -> str:
    return f"{fixture_id}|{bookmaker}|{category}"

def alert_message(
    fixture: Dict[str, Any],
    bookmaker: str,
    category: str,
    participants: Dict[str, str],
    tournament_names: Dict[str, str],
    tz_name: str,
) -> str:
    match = fixture_name(fixture, participants)
    tournament = tournament_names.get(str(fixture.get("tournamentId")), "Fútbol")
    kickoff = fmt_local_time(fixture.get("startTime"), tz_name)
    label = TARGET_LABELS.get(category, category)
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    now_local = datetime.now(tz).strftime("%H:%M:%S")

    return (
        f"🚨 NUEVO MERCADO\n\n"
        f"🏦 {bookmaker_label(bookmaker)}\n"
        f"⚽ {match}\n"
        f"🏆 {tournament}\n"
        f"🕒 Partido: {kickoff}\n\n"
        f"{label} ✅ DISPONIBLE\n\n"
        f"Detectado: {now_local}"
    )

def status_message(fixtures: List[Dict[str, Any]], cfg: Dict[str, Any]) -> str:
    cats = ", ".join(TARGET_LABELS.get(x, x) for x in cfg["targets"])
    return (
        "✅ Market Drop Bot activo\n\n"
        f"Casas: {', '.join(bookmaker_label(x) for x in cfg['bookmakers'])}\n"
        f"Partidos vigilados: {len(fixtures)}\n"
        f"Mercados: {cats}\n"
        f"Ventana: próximas {cfg['fixture_horizon_hours']} h\n\n"
        "Te aviso cuando un mercado pase de no disponible a disponible."
    )

def main() -> None:
    cfg = require_config()
    api = OddsPapi(cfg["oddspapi_key"], int(cfg["request_timeout_seconds"]))
    tg = Telegram(cfg["telegram_bot_token"], str(cfg["telegram_chat_id"]), int(cfg["request_timeout_seconds"]))

    cache = load_json(CACHE_PATH, {})
    state = load_json(STATE_PATH, {"initialized": False, "active": {}})
    reserve = int(cfg.get("quota_reserve_requests", 70))
    quota_check_seconds = max(900, int(float(cfg.get("quota_check_hours", 1)) * 3600))

    quota_alerted: Set[int] = set()
    quota_block_alerted = False
    quota_exhausted_alerted = False
    last_quota_count = -1
    next_quota_check = 0.0
    used = 0
    limit = 0

    def refresh_quota(force: bool = False) -> Tuple[int, int]:
        nonlocal used, limit, last_quota_count, next_quota_check, quota_block_alerted, quota_exhausted_alerted
        if not force and time.time() < next_quota_check:
            return used, limit
        acct = api.account()
        new_used, new_limit = quota_info(acct)
        if last_quota_count >= 0 and new_used < last_quota_count:
            quota_alerted.clear()
            quota_block_alerted = False
            quota_exhausted_alerted = False
            log(f"Cuota OddsPapi renovada: {new_used}/{new_limit}")
            try:
                tg.send(
                    f"✅ CUOTA ODDSPAPI RENOVADA\n\n"
                    f"Uso actual: {new_used}/{new_limit or '?'}\n"
                    "El bot reanuda automáticamente la vigilancia y reconstruye la lista de partidos si hace falta."
                )
            except Exception as e:
                log(f"No pude avisar renovación de cuota por Telegram: {e}")
        last_quota_count = new_used
        used, limit = new_used, new_limit
        next_quota_check = time.time() + quota_check_seconds
        return used, limit

    refresh_quota(force=True)
    log(f"Conexión con OddsPapi OK. Cuota: {used}/{limit or '?'}")

    spacing = max(float(cfg["historical_request_spacing_seconds"]), 5.05)
    targets = set(cfg["targets"])

    while True:
        try:
            refresh_quota()
            stop_at = quota_soft_stop(limit, reserve)

            # Si la cuota llegó al límite duro, OddsPapi también bloquea historical-odds.
            # No reiniciar ni quemar CPU: esperar el reset consultando solo /account, que es no medido.
            if limit > 0 and used >= limit:
                if not quota_exhausted_alerted:
                    tg.send(
                        f"⛔ CUOTA ODDSPAPI AGOTADA\n\n"
                        f"Usadas: {used}/{limit}\n"
                        "El bot queda en espera automática. No hará llamadas facturables y revisará /account hasta que la cuota se renueve."
                    )
                    quota_exhausted_alerted = True
                log(f"Cuota agotada ({used}/{limit}). Esperando renovación sin consumir requests.")
                time.sleep(min(quota_check_seconds, 3600))
                next_quota_check = 0.0
                continue

            # Avisos tempranos. El guardia real está en stop_at (180/250 con reserva 70).
            for threshold in (150, 170, stop_at):
                if threshold > 0 and used >= threshold and threshold not in quota_alerted:
                    tg.send(
                        f"⚠️ CUOTA ODDSPAPI\n\n"
                        f"Usadas: {used}/{limit or '?'}\n"
                        f"Umbral: {threshold}\n\n"
                        "El bot sigue vigilando. Las consultas facturables se frenan antes del límite para reservar cuota."
                    )
                    quota_alerted.add(threshold)

            # Metadata cuesta 3 requests. Solo se refresca si hay margen de seguridad.
            if metadata_stale(cache, int(cfg["metadata_refresh_days"])):
                if can_spend_quota(used, limit, 3, reserve):
                    cache = refresh_metadata(api, cache, cfg)
                    refresh_quota(force=True)
                elif not cache.get("market_id_to_category"):
                    if not quota_block_alerted:
                        tg.send(
                            f"🛡️ MODO AHORRO DE CUOTA\n\n"
                            f"Uso: {used}/{limit or '?'}\n"
                            "No hay margen para reconstruir el catálogo sin arriesgar el límite. Espero la renovación de cuota."
                        )
                        quota_block_alerted = True
                    time.sleep(min(quota_check_seconds, 3600))
                    next_quota_check = 0.0
                    continue

            # Para 14 días se usan 2 requests de /fixtures cada 12 h.
            fixture_cost = fixture_request_cost(int(cfg["fixture_horizon_hours"]))
            if fixture_cache_stale(cache, float(cfg["fixture_refresh_hours"])):
                if can_spend_quota(used, limit, fixture_cost, reserve):
                    cache = discover_fixtures(api, cache, cfg)
                    refresh_quota(force=True)
                elif not quota_block_alerted:
                    tg.send(
                        f"🛡️ RESERVA DE CUOTA ACTIVADA\n\n"
                        f"Uso: {used}/{limit or '?'}\n"
                        f"Reserva protegida: {reserve} requests.\n"
                        "Dejo de actualizar la lista de partidos, pero sigo consultando gratis los partidos ya cargados."
                    )
                    quota_block_alerted = True

            fixtures = cache.get("fixtures", [])
            if not fixtures:
                log("No hay partidos cargados. Espero a poder refrescar /fixtures sin comprometer la reserva.")
                time.sleep(min(quota_check_seconds, 3600))
                next_quota_check = 0.0
                continue

            # Enviar estado una sola vez por proceso, después de tener cache utilizable.
            if not state.get("status_sent"):
                tg.send(
                    status_message(fixtures, cfg)
                    + f"\n\nCuota OddsPapi: {used}/{limit or '?'} · reserva protegida: {reserve}"
                )
                state["status_sent"] = True
                save_json(STATE_PATH, state)

            id_to_category = cache.get("market_id_to_category", {})
            participants = cache.get("participants", {})
            tournament_names = cache.get("tournament_names", {})
            active_state = state.setdefault("active", {})

            for fixture in fixtures:
                fid = str(fixture.get("fixtureId"))
                if not fid:
                    continue

                try:
                    hist = api.historical(fid, cfg["bookmakers"])
                    current = extract_active_categories(hist, id_to_category, cfg["bookmakers"])

                    for book in cfg["bookmakers"]:
                        for cat in targets:
                            key = state_key(fid, book, cat)
                            prev = bool(active_state.get(key, False))
                            now = cat in current.get(book, set())

                            if not state.get("initialized") and not cfg.get("alert_on_first_seen", False):
                                active_state[key] = now
                                continue

                            if now and not prev:
                                tg.send(alert_message(
                                    fixture, book, cat, participants,
                                    tournament_names, cfg["timezone"]
                                ))
                                log(f"ALERTA: {book} {cat} {fixture_name(fixture, participants)}")

                            active_state[key] = now

                    save_json(STATE_PATH, state)

                except Exception as e:
                    msg = str(e)
                    log(f"Error consultando histórico {fid}: {e}")
                    if "REQUEST_LIMIT_EXCEEDED" in msg:
                        next_quota_check = 0.0
                        break

                time.sleep(spacing)

            if not state.get("initialized"):
                state["initialized"] = True
                save_json(STATE_PATH, state)
                log("Baseline inicial completado.")

        except KeyboardInterrupt:
            log("Bot detenido por el usuario.")
            return
        except Exception as e:
            log("Error en loop principal: " + repr(e))
            log(traceback.format_exc())
            time.sleep(30)

if __name__ == "__main__":
    main()
