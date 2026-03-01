import json
import logging
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

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
                return json.load(f)
        except json.JSONDecodeError:
            return {"monitors": {}}
    return {"monitors": {}}

def save_db(db: Dict[str, Any]) -> None:
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

# ---------------------------------------------------------------------------
# Telegram Bot Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome_text = (
        "👋 Ciao! Sono Event Tracker.\n\n"
        "Posso monitorare per te i biglietti su Eventbrite e avvisarti "
        "quando si liberano nuovi posti nei turni esauriti!\n\n"
        "📌 *Comandi disponibili:*\n"
        "Invia un link di Eventbrite per iniziare a monitorarlo.\n"
        "/list - Mostra i siti che stai monitorando e ti permette di rimuoverli."
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
        await update.message.reply_text("Non stai monitorando nessun evento al momento.")
        return
        
    text = "📋 *I tuoi monitoraggi:*\n\n"
    for mid, m_data in user_monitors:
        text += f"🔹 *{m_data['name']}*\n"
        text += f"🔗 [Link]({m_data['url']})\n"
        text += f"❌ Disattiva: /remove\_{mid}\n\n"
        
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def remove_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    # Extract ID from the command (e.g. /remove_1974872659286)
    command_text = update.message.text
    match = re.search(r'/remove_(\d+)', command_text)
    
    if not match:
        await update.message.reply_text("⚠️ Comando non valido. Clicca sui link generati da /list per rimuovere un monitoraggio.", parse_mode="Markdown")
        return
        
    target_id = match.group(1)
    db = load_db()
    
    if target_id not in db["monitors"] or chat_id not in db["monitors"][target_id].get("subscribers", []):
        await update.message.reply_text("❌ Non stai monitorando questo ID. Usa /list per vedere i tuoi ID.")
        return
        
    db["monitors"][target_id]["subscribers"].remove(chat_id)
    name = db["monitors"][target_id]["name"]
    
    # Se non c'è più nessuno che lo ascolta, potremmo anche rimuoverlo dal DB
    if not db["monitors"][target_id]["subscribers"]:
        del db["monitors"][target_id]
        
    save_db(db)
    await update.message.reply_text(f"✅ Monitoraggio rimosso per: *{name}*", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    if "eventbrite.it" not in text and "eventbrite.com" not in text:
        await update.message.reply_text("⚠️ Al momento supporto solo link da *Eventbrite*.\nInvia un link valido di un evento per iniziare il monitoraggio.", parse_mode="Markdown")
        return
        
    # Extract ID from Eventbrite URL
    match = re.search(r'(?:-|/|^)(\d+)(?:\?|$|/)', text)
    if not match:
        await update.message.reply_text("❌ Non sono riuscito a trovare l'ID dell'evento nel link. Assicurati che sia corretto.")
        return
        
    event_id = match.group(1)
    
    # Check what kind of event it is via API
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
                "url": text,
                "platform": "eventbrite",
                "name": name,
                "is_series": is_series,
                "subscribers": [chat_id],
                "events_state": {}
            }
        else:
            if chat_id not in db["monitors"][event_id]["subscribers"]:
                db["monitors"][event_id]["subscribers"].append(chat_id)
                
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
        
    logger.info("Avvio controllo schedulato...")
    db = load_db()
    modifications = False
    
    # Per ogni monitor
    for mid, m_data in dict(db["monitors"]).items():
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
        
    logger.info("Controllo terminato.")

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
    # Aggiungi handler regex per comandi dinamici come /remove_123456
    application.add_handler(MessageHandler(filters.Regex(r'^/remove_\d+$'), remove_monitor))
    
    # Ascolta qualsiasi messaggio di testo per cercare link
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Schedulazione: impostata a N ore specificate o 4 di default
    interval_hours = config.get("check_interval_hours", 4)
    interval_seconds = interval_hours * 3600
    
    job_queue = application.job_queue
    # Partiamo in ritardo di 10 secondi, e poi cicliamo
    job_queue.run_repeating(bg_check_job, interval=interval_seconds, first=10)
    
    logger.info("🤖 Event Tracker Bot avviato. In attesa di messaggi...")
    application.run_polling()

if __name__ == "__main__":
    main()
