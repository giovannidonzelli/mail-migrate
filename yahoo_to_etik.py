#!/usr/bin/env python3
"""
yahoo_to_etik.py
Sposta la posta in arrivo da Yahoo → Etik (Infomaniak) via IMAP.
Usa UID invece di sequence number — stabile anche dopo expunge mid-run.

Variabili d'ambiente richieste (GitHub Secrets):
  YAHOO_USER       es. gvadonzelli@yahoo.it
  YAHOO_PASS       App Password Yahoo (16 caratteri)
  ETIK_USER        es. giuseppe.donzelli@etik.com
  ETIK_PASS        Password Infomaniak

Autore: Claude (Polpettone) per Eagle Hack Lab / SimpleMachines.it
"""

import imaplib
import os
import sys
import time
import logging

# ── Configurazione ──────────────────────────────────────────────────────────
YAHOO_HOST  = "imap.mail.yahoo.com"
YAHOO_PORT  = 993
YAHOO_INBOX = "INBOX"

ETIK_HOST   = "mail.infomaniak.com"
ETIK_PORT   = 993
ETIK_INBOX  = "INBOX"

BATCH_SIZE  = 50
SLEEP_MS    = 300

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
    stats = {"fetched": 0, "appended": 0, "deleted": 0, "errors": 0, "skipped": 0}

    status, data = yahoo.select(YAHOO_INBOX)
    if status != "OK":
        log.error(f"Impossibile selezionare INBOX Yahoo: {data}")
        return stats

    total_msgs = int(data[0])
    log.info(f"Yahoo INBOX: {total_msgs} messaggi totali")

    if total_msgs == 0:
        log.info("Nessun messaggio da migrare.")
        return stats

    # UID SEARCH — UID stabili, non cambiano dopo expunge
    status, uid_data = yahoo.uid("SEARCH", "NOT DELETED")
    if status != "OK":
        log.error("UID SEARCH fallita")
        return stats

    uids = uid_data[0].split()
    log.info(f"Messaggi da migrare: {len(uids)} (batch max {BATCH_SIZE})")

    batch = uids[:BATCH_SIZE]

    for uid in batch:
        uid_str = uid.decode()
        try:
            # 1. UID FETCH — usa UID, non sequence number
            status, msg_data = yahoo.uid("FETCH", uid, "(RFC822 INTERNALDATE)")

            if status != "OK":
                log.warning(f"  UID FETCH status non OK per uid {uid_str}: {status}")
                stats["skipped"] += 1
                stats["errors"] += 1
                continue

            if not msg_data or msg_data[0] is None:
                log.warning(f"  msg_data vuoto per uid {uid_str}")
                stats["skipped"] += 1
                stats["errors"] += 1
                continue

            # Estrai raw_email e internal_date dalla risposta
            raw_email = None
            internal_date = None

            for part in msg_data:
                if part is None:
                    continue
                if isinstance(part, tuple):
                    header = part[0] if part[0] else b""
                    body   = part[1] if len(part) > 1 else None

                    if body and isinstance(body, bytes) and len(body) > 0:
                        raw_email = body

                    if header and b"INTERNALDATE" in header:
                        try:
                            internal_date = imaplib.Internaldate2tuple(header)
                        except Exception as e:
                            log.warning(f"  Parsing INTERNALDATE fallito: {e}")

            if raw_email is None:
                log.warning(f"  raw_email None per uid {uid_str} — struttura: {repr(msg_data)}")
                stats["skipped"] += 1
                stats["errors"] += 1
                continue

            stats["fetched"] += 1

            # 2. Append su Etik
            status, append_data = etik.append(
                ETIK_INBOX,
                None,
                internal_date,
                raw_email
            )
            if status != "OK":
                log.warning(f"  Append fallito per uid {uid_str}: {append_data}")
                stats["errors"] += 1
                continue

            stats["appended"] += 1
            log.info(f"  ✓ Migrata uid {uid_str} → Etik")

            # 3. UID STORE — cancella con UID, non sequence number
            yahoo.uid("STORE", uid, "+FLAGS", "\\Deleted")
            stats["deleted"] += 1

            time.sleep(SLEEP_MS / 1000)

        except Exception as e:
            log.error(f"Errore su uid {uid_str}: {e}")
            stats["errors"] += 1
            continue

    # 4. Expunge una sola volta alla fine
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
        log.info(f"  Skippate            : {stats['skipped']}")
        log.info(f"  Errori              : {stats['errors']}")
        log.info("─" * 50)

        # Exit 1 solo se errori > 20% del totale processato
        totale = stats["fetched"] + stats["skipped"]
        if totale > 0 and stats["errors"] / totale > 0.20:
            log.error("Troppi errori (>20%) — exit 1")
            sys.exit(1)

    finally:
        for conn in [yahoo, etik]:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
