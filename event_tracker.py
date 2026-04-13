import hashlib
import json
import logging
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Constants & Setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
DB_FILE = SCRIPT_DIR / "db.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"

API_BASE = "https://www.eventbrite.it/api/v3"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
EVENTBRITE_DOMAINS = {"eventbrite.it", "eventbrite.com", "www.eventbrite.it", "www.eventbrite.com"}

PAGE_CHECK_INTERVAL = 15 * 60  # ogni 15 minuti

# Shortcut commands: pagine preconfigurate attivabili con /<id>
PAGE_SHORTCUTS = [
    {
        "id": "prada",
        "name": "Prada Frames 2026",
        "url": "https://www.prada.com/it/it/pradasphere/events/2026/prada-frames-milan.html",
    },
    {
        "id": "furla",
        "name": "Furla Design Week",
        "url": "https://www.furla.com/it/it/eshop/collections/design-week/",
    },
]
PAGE_SHORTCUTS_BY_ID = {s["id"]: s for s in PAGE_SHORTCUTS}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database Management
# ---------------------------------------------------------------------------
def load_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        try:
            with open(DB_FILE, "r") as f:
                db = json.load(f)
        except json.JSONDecodeError:
            return {"monitors": {}}
    else:
        return {"monitors": {}}

    db.setdefault("monitors", {})

    # Migrazione: vecchie chiavi page_watch_*_subscribers -> monitors unificati
    migrated = False
    for shortcut in PAGE_SHORTCUTS:
        old_subs_key = f"page_watch_{shortcut['id']}_subscribers"
        old_hash_key = f"page_watch_{shortcut['id']}_hash"
        if old_subs_key in db:
            subs = db.pop(old_subs_key)
            old_hash = db.pop(old_hash_key, None)
            mid = _make_page_watcher_id(shortcut["url"])
            if mid not in db["monitors"]:
                db["monitors"][mid] = {
                    "url": shortcut["url"],
                    "platform": "page_watcher",
                    "name": shortcut["name"],
                    "subscribers": subs,
                }
                if old_hash:
                    db["monitors"][mid]["page_hash"] = old_hash
            else:
                for s in subs:
                    if s not in db["monitors"][mid]["subscribers"]:
                        db["monitors"][mid]["subscribers"].append(s)
            migrated = True
    # Pulizia vecchie chiavi
    for key in list(db.keys()):
        if key.startswith("prada_") or key.startswith("page_watch_"):
            db.pop(key)
            migrated = True
    if migrated:
        save_db(db)
    return db

def save_db(db: Dict[str, Any]) -> None:
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def _make_page_watcher_id(url: str) -> str:
    """Genera un ID stabile per un page watcher basato sull'URL."""
    return hashlib.md5(url.encode()).hexdigest()[:12]

# ---------------------------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    shortcut_lines = "\n".join(
        f"/{s['id']} - Monitora {s['name']}." for s in PAGE_SHORTCUTS
    )
    welcome_text = (
        "👋 Ciao! Sono Event Tracker.\n\n"
        "Posso monitorare per te pagine web e biglietti Eventbrite!\n\n"
        "📌 *Comandi disponibili:*\n"
        "Invia un *link Eventbrite* → monitoraggio posti liberi.\n"
        "Invia un *qualsiasi altro link* → ti avviso quando la pagina cambia.\n"
        "/list - Mostra i tuoi monitoraggi e permette di rimuoverli.\n"
        f"{shortcut_lines}"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def list_monitors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db = load_db()

    user_monitors = []
    for mid, m_data in db["monitors"].items():
        if chat_id in m_data.get("subscribers", []):
            user_monitors.append((mid, m_data))

    if not user_monitors:
        await update.message.reply_text("Non stai monitorando nulla al momento.")
        return

    text = "📋 *I tuoi monitoraggi:*\n\n"
    for mid, m_data in user_monitors:
        platform = m_data.get("platform", "eventbrite")
        icon = "🌐" if platform == "page_watcher" else "🎫"
        text += f"{icon} *{m_data['name']}*\n"
        text += f"🔗 [Link]({m_data['url']})\n"
        text += f"❌ Rimuovi: /remove\\_{mid}\n\n"

    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def remove_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    command_text = update.message.text
    match = re.search(r'/remove_(\w+)', command_text)

    if not match:
        await update.message.reply_text("⚠️ Comando non valido. Usa /list per vedere i tuoi monitoraggi.")
        return

    target_id = match.group(1)
    db = load_db()

    if target_id not in db["monitors"] or chat_id not in db["monitors"][target_id].get("subscribers", []):
        await update.message.reply_text("❌ Non stai monitorando questo ID. Usa /list per vedere i tuoi monitoraggi.")
        return

    db["monitors"][target_id]["subscribers"].remove(chat_id)
    name = db["monitors"][target_id]["name"]

    if not db["monitors"][target_id]["subscribers"]:
        del db["monitors"][target_id]

    save_db(db)
    await update.message.reply_text(f"✅ Monitoraggio rimosso per: *{name}*", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Page Watcher helpers
# ---------------------------------------------------------------------------
def _add_page_watcher(db: Dict, chat_id: int, url: str, name: str) -> str:
    """Aggiunge o iscrive l'utente a un page watcher. Ritorna 'added' o 'exists'."""
    mid = _make_page_watcher_id(url)
    if mid not in db["monitors"]:
        db["monitors"][mid] = {
            "url": url,
            "platform": "page_watcher",
            "name": name,
            "subscribers": [chat_id],
        }
        return "added"
    else:
        if chat_id not in db["monitors"][mid]["subscribers"]:
            db["monitors"][mid]["subscribers"].append(chat_id)
            return "added"
        return "exists"


async def page_shortcut_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gestisce i comandi shortcut (/prada, /furla, ...). Iscrive senza toggle."""
    command = update.message.text.strip().lstrip('/')
    shortcut = PAGE_SHORTCUTS_BY_ID.get(command)
    if not shortcut:
        await update.message.reply_text("⚠️ Comando non riconosciuto.")
        return

    chat_id = update.effective_chat.id
    db = load_db()
    result = _add_page_watcher(db, chat_id, shortcut["url"], shortcut["name"])
    save_db(db)

    if result == "exists":
        await update.message.reply_text(
            f"ℹ️ Stai già monitorando *{shortcut['name']}*.\n"
            "Usa /list per gestire i tuoi monitoraggi.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Ti avviserò quando la pagina *{shortcut['name']}* viene modificata!\n\n"
            f"🔗 [Pagina]({shortcut['url']})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )


# ---------------------------------------------------------------------------
# Handle incoming messages (links)
# ---------------------------------------------------------------------------
def _is_eventbrite_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in EVENTBRITE_DOMAINS
    except Exception:
        return False

def _extract_url(text: str) -> Optional[str]:
    """Estrae il primo URL http/https dal testo."""
    m = re.search(r'https?://\S+', text)
    return m.group(0) if m else None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    url = _extract_url(text)
    if not url:
        await update.message.reply_text("⚠️ Invia un link valido (http/https) per iniziare il monitoraggio.")
        return

    if _is_eventbrite_url(url):
        await _handle_eventbrite(update, context, url, chat_id)
    else:
        await _handle_page_watcher(update, context, url, chat_id)


async def _handle_page_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int) -> None:
    """Aggiunge un URL generico come page watcher."""
    parsed = urlparse(url)
    name = parsed.hostname or url[:50]

    db = load_db()
    result = _add_page_watcher(db, chat_id, url, name)
    save_db(db)

    if result == "exists":
        await update.message.reply_text(
            "ℹ️ Stai già monitorando questa pagina.\n"
            "Usa /list per gestire i tuoi monitoraggi.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Monitoraggio attivato!\n\n"
            f"🌐 *{name}*\n"
            "Ti avviserò quando la pagina viene modificata.\n\n"
            "Usa /list per gestire i tuoi monitoraggi.",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )


async def _handle_eventbrite(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, chat_id: int) -> None:
    """Gestisce un link Eventbrite."""
    match = re.search(r'(?:-|/|^)(\d+)(?:\?|$|/)', url)
    if not match:
        await update.message.reply_text("❌ Non sono riuscito a trovare l'ID dell'evento nel link.")
        return

    event_id = match.group(1)
    await update.message.reply_text("🔍 Controllo l'evento, un attimo...")

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        resp = session.get(f"{API_BASE}/destination/events/?event_ids={event_id}", timeout=15)
        resp.raise_for_status()

        data = resp.json()
        if not data.get("events"):
            await update.message.reply_text("❌ Evento non trovato. Potrebbe essere privato, scaduto o il link non è corretto.")
            return

        ev_info = data["events"][0]
        name = ev_info.get("name", "Evento Sconosciuto")
        is_series = (str(ev_info.get("series_id", "")) == str(event_id))

        db = load_db()

        if event_id not in db["monitors"]:
            db["monitors"][event_id] = {
                "url": url,
                "platform": "eventbrite",
                "name": name,
                "is_series": is_series,
                "subscribers": [chat_id],
                "events_state": {}
            }
        else:
            if chat_id not in db["monitors"][event_id]["subscribers"]:
                db["monitors"][event_id]["subscribers"].append(chat_id)
            else:
                await update.message.reply_text(
                    f"ℹ️ Stai già monitorando *{name}*.\n"
                    "Usa /list per gestire i tuoi monitoraggi.",
                    parse_mode="Markdown"
                )
                return

        save_db(db)

        msg = f"✅ Aggiunto con successo!\n\nSto monitorando: *{name}*\n"
        if is_series:
            msg += "Questo è un evento ricorrente, controllerò tutte le date future in cerca di disdette e posti liberi."
        else:
            msg += "Questo è un evento singolo, ti avviserò se tornano posti."

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Errore durante l'aggiunta dell'evento: {e}")
        await update.message.reply_text("❌ Si è verificato un errore comunicando con Eventbrite. Riprova più tardi.")

# ---------------------------------------------------------------------------
# Background Checking Logic
# ---------------------------------------------------------------------------

# -- Page watcher hash check --

def get_page_hash(url: str) -> Optional[str]:
    """Fetch una pagina web e ritorna un hash del contenuto principale."""
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        html = resp.text

        # Rimuovi parti dinamiche che cambiano ad ogni request
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
        html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
        html = re.sub(r'\s(?:nonce|data-nonce|csrf|data-timestamp)=["\'][^"\']*["\']', '', html)
        html = re.sub(r'\s+', ' ', html).strip()

        return hashlib.sha256(html.encode('utf-8')).hexdigest()
    except Exception as e:
        logger.error(f"Errore controllo pagina {url}: {e}")
        return None


async def bg_page_watch_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Controllo periodico di tutti i page watcher in monitors."""
    now = datetime.now()

    if now.hour >= 23 or now.hour < 7:
        logger.info("Page watchers: orario notturno, skip.")
        return

    db = load_db()
    modifications = False

    for mid, m_data in dict(db["monitors"]).items():
        if m_data.get("platform") != "page_watcher":
            continue
        if not m_data.get("subscribers"):
            continue

        url = m_data["url"]
        name = m_data["name"]
        logger.info(f"Controllo pagina {name} ({url[:60]})...")

        current_hash = get_page_hash(url)
        if current_hash is None:
            continue

        prev_hash = m_data.get("page_hash")
        if prev_hash is None:
            m_data["page_hash"] = current_hash
            modifications = True
            logger.info(f"{name}: hash iniziale salvato ({current_hash[:12]}…)")
            continue

        if current_hash != prev_hash:
            logger.info(f"🔔 Pagina {name} modificata! ({prev_hash[:12]}… → {current_hash[:12]}…)")
            m_data["page_hash"] = current_hash
            modifications = True

            msg = (
                f"🔔 *La pagina {name} è stata aggiornata!*\n\n"
                f"👉 [Vai alla pagina]({url})"
            )
            for sub in m_data["subscribers"]:
                try:
                    await context.bot.send_message(
                        chat_id=sub,
                        text=msg,
                        parse_mode="Markdown",
                        disable_web_page_preview=True
                    )
                except Exception as e:
                    logger.error(f"Errore notifica {name} a {sub}: {e}")
        else:
            logger.info(f"{name}: nessuna modifica.")

        time.sleep(2)

    if modifications:
        save_db(db)


# -- Eventbrite monitors --
def check_event_series(series_id: str) -> List[Dict]:
    events = []
    url = f"{API_BASE}/series/{series_id}/events/?time_filter=current_future&page_size=50&expand=ticket_availability"
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    while url:
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            for ev in data.get("events", []):
                if ev["status"] != "live":
                    continue
                tickets_info = ev.get("ticket_availability", {})
                is_available = tickets_info.get("has_available_tickets", False)
                events.append({
                    "id": ev["id"],
                    "url": ev["url"],
                    "start": ev["start"]["local"],
                    "is_available": is_available,
                })
                
            pagination = data.get("pagination", {})
            if pagination.get("has_more_items"):
                continuation = pagination.get("continuation", "")
                url = f"{API_BASE}/series/{series_id}/events/?time_filter=current_future&page_size=50&expand=ticket_availability&continuation={continuation}"
                time.sleep(1)
            else:
                url = None
        except Exception as e:
            logger.error(f"Errore controllo serie {series_id}: {e}")
            break
            
    return events

def check_single_event(event_id: str) -> List[Dict]:
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        resp = session.get(f"{API_BASE}/destination/events/?event_ids={event_id}&expand=ticket_availability", timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("events"):
            return []
            
        ev = data["events"][0]
        if ev["status"] != "live":
            return []
            
        tickets_info = ev.get("ticket_availability", {})
        is_available = tickets_info.get("has_available_tickets", False)
        
        return [{
            "id": ev["id"],
            "url": ev["url"],
            "start": ev["start_date"] + "T" + ev.get("start_time", "00:00:00"),
            "is_available": is_available,
        }]
    except Exception as e:
        logger.error(f"Errore controllo evento {event_id}: {e}")
        return []

async def bg_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now()
    
    # Orari notturni (es. dalle 22:00 alle 07:00 incluse) non controlliamo
    if now.hour >= 22 or now.hour <= 7:
        logger.info("Skip controllo: orario notturno.")
        return
        
    logger.info("Avvio controllo Eventbrite schedulato...")
    db = load_db()
    modifications = False
    
    # Per ogni monitor
    for mid, m_data in dict(db["monitors"]).items():
        if m_data.get("platform") == "page_watcher":
            continue
        if not m_data.get("subscribers"):
            continue
            
        logger.info(f"Controllo: {m_data['name']} ({mid})")
        
        if m_data.get("is_series", False):
            results = check_event_series(mid)
        else:
            results = check_single_event(mid)
            
        # Analizziamo i risultati e mandiamo notifiche se qualcosa è tornato disponibile
        states = m_data.get("events_state", {})
        
        for r in results:
            cid = r["id"]
            curr_avail = r["is_available"]
            prev_avail = states.get(cid, {}).get("available", None)
            
            # Se prima non era disponibile (o non era tracciato) e ORA è disponibile -> MANDA NOTIFICA
            # NOTA: se è il primissimo controllo (prev_avail è None) vogliamo inviare?
            # Meglio di no: potremmo inondare l'utente con tutti gli eventi disponibili appena mette il link.
            # Lo inviamo solo se da False diventa True.
            
            if curr_avail is True and prev_avail is False:
                # E' diventato disponibile!
                logger.info(f"🔔 Cambio di stato per {m_data['name']}! Data: {r['start']}")
                
                dt = ""
                try:
                    dt_obj = datetime.fromisoformat(r["start"])
                    dt = dt_obj.strftime("%d/%m/%Y %H:%M")
                except:
                    dt = r["start"]
                    
                msg = (
                    f"🎉 *Nuovi posti disponibili!*\n\n"
                    f"🔹 *{m_data['name']}*\n"
                    f"📅 Data: {dt}\n\n"
                    f"👉 [Prenota Subito]({r['url']})"
                )
                
                for sub in m_data["subscribers"]:
                    try:
                        await context.bot.send_message(
                            chat_id=sub, 
                            text=msg, 
                            parse_mode="Markdown",
                            disable_web_page_preview=True
                        )
                    except Exception as e:
                        logger.error(f"Errore notifica a {sub}: {e}")
                        
            # Aggiorniamo stato
            if cid not in states or states[cid].get("available") != curr_avail:
                states[cid] = {
                    "available": curr_avail,
                    "url": r["url"],
                    "start": r["start"]
                }
                modifications = True
                
        m_data["events_state"] = states
        
        # Facciamo una piccola pausa tra le chiamate per non esagerare se abbiamo molti monitoraggi
        time.sleep(2)
        
    if modifications:
        save_db(db)
        
    logger.info("Controllo Eventbrite terminato.")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not CONFIG_FILE.exists():
        print("❌ File config.json non trovato!")
        print("Crealo inserendo:")
        print('{\n  "telegram_bot_token": "IL_TUO_TOKEN_QUI",\n  "check_interval_hours": 4\n}')
        return
        
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
        
    token = config.get("telegram_bot_token", "")
    if not token or token == "IL_TUO_TOKEN_QUI":
        print("❌ Token Telegram non configurato in config.json!")
        return
        
    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_monitors))
    # Shortcut per page watchers preconfigurati
    for s in PAGE_SHORTCUTS:
        application.add_handler(CommandHandler(s["id"], page_shortcut_command))
    # Comandi dinamici /remove_<id>
    application.add_handler(MessageHandler(filters.Regex(r'^/remove_\w+$'), remove_monitor))
    
    # Ascolta qualsiasi messaggio di testo per link
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Eventbrite: check ogni N ore
    interval_hours = config.get("check_interval_hours", 4)
    interval_seconds = interval_hours * 3600
    
    job_queue = application.job_queue
    # Partiamo in ritardo di 10 secondi, e poi cicliamo
    job_queue.run_repeating(bg_check_job, interval=interval_seconds, first=10)

    # Page watchers: check ogni 15 minuti
    job_queue.run_repeating(bg_page_watch_check, interval=PAGE_CHECK_INTERVAL, first=30)
    
    logger.info("🤖 Event Tracker Bot avviato. In attesa di messaggi...")
    application.run_polling()

if __name__ == "__main__":
    main()
