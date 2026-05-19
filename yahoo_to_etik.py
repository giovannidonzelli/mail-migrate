#!/usr/bin/env python3
"""
yahoo_to_etik.py
Sposta la posta in arrivo da Yahoo → Etik (Infomaniak) via IMAP.
Ogni email viene appended su Etik e poi expunged da Yahoo.
Sicuro: nessun SMTP, nessun relay. Solo IMAP append.

Variabili d'ambiente richieste (GitHub Secrets):
  YAHOO_USER       es. giuseppe.donzelli@yahoo.it
  YAHOO_PASS       App Password Yahoo (16 caratteri)
  ETIK_USER        es. giuseppe.donzelli@etik.com
  ETIK_PASS        Password Infomaniak

Autore: Claude (Polpettone) per Eagle Hack Lab / SimpleMachines.it
"""

import imaplib
import os
import sys
import time
import email
import logging
from datetime import datetime

# ── Configurazione ──────────────────────────────────────────────────────────
YAHOO_HOST  = "imap.mail.yahoo.com"
YAHOO_PORT  = 993
YAHOO_INBOX = "INBOX"

ETIK_HOST   = "mail.infomaniak.com"
ETIK_PORT   = 993
ETIK_INBOX  = "INBOX"

BATCH_SIZE  = 50   # email per run (sicuro contro timeout GitHub Actions)
SLEEP_MS    = 200  # ms tra un append e l'altro (gentile col server)

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def get_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log.error(f"Variabile d'ambiente mancante: {name}")
        sys.exit(1)
    return val


def connect_imap(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    log.info(f"Connessione IMAP → {host}:{port} come {user}")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    log.info(f"Login OK: {user}")
    return conn


def migrate(yahoo: imaplib.IMAP4_SSL, etik: imaplib.IMAP4_SSL) -> dict:
    stats = {"fetched": 0, "appended": 0, "deleted": 0, "errors": 0}

    # Seleziona INBOX Yahoo (read-write per poter cancellare)
    status, data = yahoo.select(YAHOO_INBOX)
    if status != "OK":
        log.error(f"Impossibile selezionare INBOX Yahoo: {data}")
        return stats

    total_msgs = int(data[0])
    log.info(f"Yahoo INBOX: {total_msgs} messaggi totali")

    if total_msgs == 0:
        log.info("Nessun messaggio da migrare.")
        return stats

    # Cerca tutti i messaggi non cancellati
    status, msg_ids_raw = yahoo.search(None, "NOT DELETED")
    if status != "OK":
        log.error("Ricerca messaggi fallita")
        return stats

    msg_ids = msg_ids_raw[0].split()
    log.info(f"Messaggi da migrare: {len(msg_ids)} (batch max {BATCH_SIZE})")

    # Prende solo il batch corrente
    batch = msg_ids[:BATCH_SIZE]

    for msg_id in batch:
        try:
            # 1. Fetch email completa (RFC822 = raw bytes)
            status, msg_data = yahoo.fetch(msg_id, "(RFC822 INTERNALDATE)")
            if status != "OK":
                log.warning(f"Fetch fallito per msg {msg_id}")
                stats["errors"] += 1
                continue

            raw_email = msg_data[0][1]
            # Prende la data originale per preservarla su Etik
            internal_date_str = msg_data[0][0].decode()
            internal_date = imaplib.Internaldate2tuple(msg_data[0][0])
            stats["fetched"] += 1

            # 2. Append su Etik con data originale
            status, append_data = etik.append(
                ETIK_INBOX,
                None,           # flags (nessuno — arriva come non letto)
                internal_date,  # data originale preservata
                raw_email
            )
            if status != "OK":
                log.warning(f"Append fallito per msg {msg_id}: {append_data}")
                stats["errors"] += 1
                continue

            stats["appended"] += 1
            log.info(f"  ✓ Migrata msg {msg_id.decode()} → Etik")

            # 3. Marca come \Deleted su Yahoo
            yahoo.store(msg_id, "+FLAGS", "\\Deleted")
            stats["deleted"] += 1

            time.sleep(SLEEP_MS / 1000)

        except Exception as e:
            log.error(f"Errore su msg {msg_id}: {e}")
            stats["errors"] += 1
            continue

    # 4. Expunge: rimuove definitivamente i messaggi marcati \Deleted
    if stats["deleted"] > 0:
        log.info("Expunge Yahoo INBOX...")
        yahoo.expunge()
        log.info("Expunge completato.")

    return stats


def main():
    yahoo_user = get_env("YAHOO_USER")
    yahoo_pass = get_env("YAHOO_PASS")
    etik_user  = get_env("ETIK_USER")
    etik_pass  = get_env("ETIK_PASS")

    yahoo = None
    etik  = None

    try:
        yahoo = connect_imap(YAHOO_HOST, YAHOO_PORT, yahoo_user, yahoo_pass)
        etik  = connect_imap(ETIK_HOST,  ETIK_PORT,  etik_user,  etik_pass)

        stats = migrate(yahoo, etik)

        log.info("─" * 50)
        log.info(f"RISULTATO:")
        log.info(f"  Recuperate da Yahoo : {stats['fetched']}")
        log.info(f"  Copiate su Etik     : {stats['appended']}")
        log.info(f"  Cancellate da Yahoo : {stats['deleted']}")
        log.info(f"  Errori              : {stats['errors']}")
        log.info("─" * 50)

        if stats["errors"] > 0:
            sys.exit(1)  # Fa fallire il GitHub Actions run → notifica

    finally:
        for conn in [yahoo, etik]:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
