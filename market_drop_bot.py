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
    "fixture_horizon_hours": 48,
    "fixture_refresh_hours": 6,
    "metadata_refresh_days": 7,
    "historical_request_spacing_seconds": 5.3,
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
        r = self.s.get(BASE + endpoint, params=params, timeout=self.timeout)
        if r.status_code == 429:
            raise RuntimeError("OddsPapi devolvió 429: cuota agotada o rate limit.")
        r.raise_for_status()
        return r.json()

    def account(self) -> Any:
        return self.get("/account")

    def markets(self) -> Any:
        return self.get("/markets", {"language": "en"})

    def tournaments(self) -> Any:
        return self.get("/tournaments", {"sportId": 10, "language": "en"})

    def participants(self) -> Any:
        return self.get("/participants", {"sportId": 10, "language": "en"})

    def fixtures(self, hours: int, bookmakers: List[str]) -> Any:
        now = datetime.now(timezone.utc)
        to = now + timedelta(hours=hours)
        return self.get("/fixtures", {
            "sportId": 10,
            "from": now.isoformat().replace("+00:00", "Z"),
            "to": to.isoformat().replace("+00:00", "Z"),
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
        r = self.s.post(
            f"{TG_BASE}/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            timeout=self.timeout,
        )
        r.raise_for_status()

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
        "participants": participants,
    })
    save_json(CACHE_PATH, cache)
    log(f"Mercados player-prop mapeados: {len(id_to_category)}")
    log(f"Torneos vigilados: {len(selected_ids)}")
    return cache

def discover_fixtures(api: OddsPapi, cache: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    log("Actualizando lista de partidos...")
    data = api.fixtures(int(cfg["fixture_horizon_hours"]), cfg["bookmakers"])
    fixtures = as_fixture_list(data)
    selected_ids = set(int(x) for x in cache.get("selected_tournament_ids", []))
    if selected_ids:
        fixtures = [f for f in fixtures if int(f.get("tournamentId", -1)) in selected_ids]
    fixtures.sort(key=lambda x: str(x.get("startTime") or ""))

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

    api.account()
    log("Conexión con OddsPapi OK.")

    if metadata_stale(cache, int(cfg["metadata_refresh_days"])):
        cache = refresh_metadata(api, cache, cfg)

    if fixture_cache_stale(cache, float(cfg["fixture_refresh_hours"])):
        cache = discover_fixtures(api, cache, cfg)

    tg.send(status_message(cache.get("fixtures", []), cfg))

    spacing = max(float(cfg["historical_request_spacing_seconds"]), 5.05)
    targets = set(cfg["targets"])

    while True:
        try:
            if metadata_stale(cache, int(cfg["metadata_refresh_days"])):
                cache = refresh_metadata(api, cache, cfg)
                time.sleep(1.1)

            if fixture_cache_stale(cache, float(cfg["fixture_refresh_hours"])):
                cache = discover_fixtures(api, cache, cfg)
                time.sleep(1.1)

            fixtures = cache.get("fixtures", [])
            if not fixtures:
                log("No hay partidos en vigilancia. Espero 10 minutos.")
                time.sleep(600)
                continue

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
                    log(f"Error consultando histórico {fid}: {e}")

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
