#!/usr/bin/env python3
"""
yahoo_to_etik.py
Sposta la posta in arrivo da Yahoo -> Etik (Infomaniak) via IMAP.
Architettura multiutente: processa tutti gli utenti configurati in sequenza.
Usa UID invece di sequence number — stabile anche dopo expunge mid-run.

Convenzione secrets (GitHub):
  Utente base (primo):   YAHOO_USER, YAHOO_PASS, ETIK_USER, ETIK_PASS
  Utenti aggiuntivi:     YAHOO_USER_2, YAHOO_PASS_2, ETIK_USER_2, ETIK_PASS_2
                         YAHOO_USER_3, YAHOO_PASS_3, ETIK_USER_3, ETIK_PASS_3
                         ... fino a N

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


def get_users() -> list[dict]:
    """
    Legge tutti gli utenti configurati dai secrets.
    Primo utente: YAHOO_USER (senza numero) — secrets originali invariati.
    Utenti aggiuntivi: YAHOO_USER_2, YAHOO_USER_3, ... fino a che non trova il prossimo.
    """
    users = []

    # Utente base — secrets senza numero
    yahoo_user = os.environ.get("YAHOO_USER", "").strip()
    yahoo_pass = os.environ.get("YAHOO_PASS", "").strip()
    etik_user  = os.environ.get("ETIK_USER",  "").strip()
    etik_pass  = os.environ.get("ETIK_PASS",  "").strip()

    if yahoo_user and all([yahoo_pass, etik_user, etik_pass]):
        users.append({
            "index":      1,
            "yahoo_user": yahoo_user,
            "yahoo_pass": yahoo_pass,
            "etik_user":  etik_user,
            "etik_pass":  etik_pass,
        })
    elif yahoo_user:
        log.warning("Utente base (YAHOO_USER) — secrets incompleti, skippato.")

    # Utenti aggiuntivi — numerati da 2 in poi
    i = 2
    while True:
        yahoo_user = os.environ.get(f"YAHOO_USER_{i}", "").strip()
        yahoo_pass = os.environ.get(f"YAHOO_PASS_{i}", "").strip()
        etik_user  = os.environ.get(f"ETIK_USER_{i}",  "").strip()
        etik_pass  = os.environ.get(f"ETIK_PASS_{i}",  "").strip()

        if not yahoo_user:
            break  # Nessun altro utente configurato

        if not all([yahoo_pass, etik_user, etik_pass]):
            log.warning(f"Utente {i} ({yahoo_user}) — secrets incompleti, skippato.")
            i += 1
            continue

        users.append({
            "index":      i,
            "yahoo_user": yahoo_user,
            "yahoo_pass": yahoo_pass,
            "etik_user":  etik_user,
            "etik_pass":  etik_pass,
        })
        i += 1

    return users


def connect_imap(host: str, port: int, user: str, password: str) -> imaplib.IMAP4_SSL:
    log.info(f"  Connessione IMAP -> {host} come {user}")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    log.info(f"  Login OK: {user}")
    return conn


def migrate_user(user: dict) -> dict:
    stats = {"fetched": 0, "appended": 0, "deleted": 0, "errors": 0, "skipped": 0}

    yahoo = None
    etik  = None

    try:
        yahoo = connect_imap(YAHOO_HOST, YAHOO_PORT, user["yahoo_user"], user["yahoo_pass"])
        etik  = connect_imap(ETIK_HOST,  ETIK_PORT,  user["etik_user"],  user["etik_pass"])

        status, data = yahoo.select(YAHOO_INBOX)
        if status != "OK":
            log.error(f"  Impossibile selezionare INBOX Yahoo: {data}")
            return stats

        total_msgs = int(data[0])
        log.info(f"  Yahoo INBOX: {total_msgs} messaggi totali")

        if total_msgs == 0:
            log.info("  Nessun messaggio da migrare.")
            return stats

        status, uid_data = yahoo.uid("SEARCH", "NOT DELETED")
        if status != "OK":
            log.error("  UID SEARCH fallita")
            return stats

        uids = uid_data[0].split()
        log.info(f"  Messaggi da migrare: {len(uids)} (batch max {BATCH_SIZE})")

        batch = uids[:BATCH_SIZE]

        for uid in batch:
            uid_str = uid.decode()
            try:
                status, msg_data = yahoo.uid("FETCH", uid, "(RFC822 INTERNALDATE)")

                if status != "OK" or not msg_data or msg_data[0] is None:
                    log.warning(f"  Fetch fallito per uid {uid_str}")
                    stats["skipped"] += 1
                    stats["errors"] += 1
                    continue

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
                            except Exception:
                                pass

                if raw_email is None:
                    log.warning(f"  raw_email None per uid {uid_str}")
                    stats["skipped"] += 1
                    stats["errors"] += 1
                    continue

                stats["fetched"] += 1

                status, append_data = etik.append(
                    ETIK_INBOX, None, internal_date, raw_email
                )
                if status != "OK":
                    log.warning(f"  Append fallito per uid {uid_str}: {append_data}")
                    stats["errors"] += 1
                    continue

                stats["appended"] += 1
                log.info(f"  ✓ uid {uid_str} -> Etik")

                yahoo.uid("STORE", uid, "+FLAGS", "\\Deleted")
                stats["deleted"] += 1

                time.sleep(SLEEP_MS / 1000)

            except Exception as e:
                log.error(f"  Errore uid {uid_str}: {e}")
                stats["errors"] += 1
                continue

        if stats["deleted"] > 0:
            log.info("  Expunge Yahoo INBOX...")
            yahoo.expunge()
            log.info("  Expunge completato.")

    finally:
        for conn in [yahoo, etik]:
            if conn:
                try:
                    conn.logout()
                except Exception:
                    pass

    return stats


def main():
    users = get_users()

    if not users:
        log.error("Nessun utente configurato — verifica i secrets YAHOO_USER ecc.")
        sys.exit(1)

    log.info(f"Utenti configurati: {len(users)}")

    total_errors    = 0
    total_processed = 0

    for user in users:
        log.info("=" * 50)
        log.info(f"UTENTE {user['index']}: {user['yahoo_user']} -> {user['etik_user']}")
        log.info("=" * 50)

        stats = migrate_user(user)

        log.info(f"  Recuperate : {stats['fetched']}")
        log.info(f"  Copiate    : {stats['appended']}")
        log.info(f"  Cancellate : {stats['deleted']}")
        log.info(f"  Skippate   : {stats['skipped']}")
        log.info(f"  Errori     : {stats['errors']}")

        total_errors    += stats["errors"]
        total_processed += stats["fetched"] + stats["skipped"]

    log.info("=" * 50)
    log.info(f"TOTALE errori: {total_errors} su {total_processed} messaggi")
    log.info("=" * 50)

    if total_processed > 0 and total_errors / total_processed > 0.20:
        log.error("Troppi errori (>20%) — exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
