import imaplib
import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr


IMAP_HOST = "imap.mail.me.com"
IMAP_PORT = 993

ICLOUD_EMAIL = os.environ["ICLOUD_EMAIL"].strip()
ICLOUD_APP_PASSWORD = os.environ["ICLOUD_APP_PASSWORD"].strip()

BLACKLIST_FOLDER = "Blacklist"
BLACKLIST_FILE = Path("blacklist.txt")

# Inbox / Junk / Trash 每次檢查最近 7 日郵件。
# 即使 GitHub Actions 偶爾延遲，也不容易漏掉。
LOOKBACK_DAYS = 7


def normalize_email(address):
    return str(address or "").strip().lower()


def load_blacklist():
    if not BLACKLIST_FILE.exists():
        return set()

    return {
        normalize_email(line)
        for line in BLACKLIST_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if normalize_email(line)
    }


def save_blacklist(blacklist):
    content = "\n".join(sorted(blacklist))

    if content:
        content += "\n"

    BLACKLIST_FILE.write_text(
        content,
        encoding="utf-8"
    )


def connect():
    print("Connecting to iCloud IMAP...")

    mail = imaplib.IMAP4_SSL(
        IMAP_HOST,
        IMAP_PORT
    )

    mail.login(
        ICLOUD_EMAIL,
        ICLOUD_APP_PASSWORD
    )

    print("iCloud IMAP login successful.")

    return mail


def list_mailboxes(mail):
    status, data = mail.list()

    if status != "OK":
        raise RuntimeError(
            "Unable to list iCloud mailboxes."
        )

    folders = []

    pattern = re.compile(
        r'^\((.*?)\)\s+(?:"([^"]*)"|NIL)\s+(.+)$'
    )

    for raw in data or []:
        if not raw:
            continue

        line = raw.decode(
            "utf-8",
            errors="replace"
        )

        match = pattern.match(line)

        if not match:
            print(
                "Unable to parse mailbox:",
                line
            )
            continue

        flags = match.group(1)
        name = match.group(3).strip()

        if (
            len(name) >= 2
            and name.startswith('"')
            and name.endswith('"')
        ):
            name = name[1:-1]

        folders.append({
            "name": name,
            "flags": flags
        })

    return folders


def find_folder_by_flag(
    folders,
    flag,
    fallback_names=None
):
    flag = flag.lower()

    for folder in folders:
        flags = folder["flags"].lower()

        if flag in flags:
            return folder["name"]

    fallback_names = fallback_names or []

    for wanted in fallback_names:
        for folder in folders:
            if (
                folder["name"].lower()
                == wanted.lower()
            ):
                return folder["name"]

    return None


def find_folder_by_name(
    folders,
    wanted
):
    for folder in folders:
        if (
            folder["name"].lower()
            == wanted.lower()
        ):
            return folder["name"]

    return None


def quote_folder(folder):
    escaped = folder.replace(
        "\\",
        "\\\\"
    ).replace(
        '"',
        '\\"'
    )

    return f'"{escaped}"'


def select_folder(
    mail,
    folder
):
    status, data = mail.select(
        quote_folder(folder),
        readonly=False
    )

    if status != "OK":
        raise RuntimeError(
            f"Unable to open mailbox: {folder}"
        )

    return data


def get_sender(mail, uid):
    status, data = mail.uid(
        "fetch",
        uid,
        "(BODY.PEEK[HEADER.FIELDS (FROM)])"
    )

    if status != "OK":
        return ""

    raw_header = b""

    for item in data or []:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(
                item[1],
                bytes
            )
        ):
            raw_header += item[1]

    if not raw_header:
        return ""

    message = BytesParser(
        policy=policy.default
    ).parsebytes(raw_header)

    sender = parseaddr(
        message.get("From", "")
    )[1]

    return normalize_email(sender)


def mark_deleted(
    mail,
    uid
):
    status, _ = mail.uid(
        "store",
        uid,
        "+FLAGS.SILENT",
        r"(\Deleted)"
    )

    if status != "OK":
        raise RuntimeError(
            f"Unable to mark message {uid} deleted."
        )


def ensure_blacklist_folder(
    mail,
    folders
):
    existing = find_folder_by_name(
        folders,
        BLACKLIST_FOLDER
    )

    if existing:
        return existing

    print(
        "Blacklist folder not found. "
        "Creating it..."
    )

    status, _ = mail.create(
        quote_folder(
            BLACKLIST_FOLDER
        )
    )

    if status != "OK":
        raise RuntimeError(
            "Unable to create Blacklist folder."
        )

    return BLACKLIST_FOLDER


def import_blacklist_folder(
    mail,
    folder,
    blacklist
):
    """
    Every message moved manually into
    Blacklist means:
      1. remember sender
      2. permanently delete message
    """

    print(
        f"Scanning manual Blacklist folder: {folder}"
    )

    select_folder(
        mail,
        folder
    )

    status, data = mail.uid(
        "search",
        None,
        "ALL"
    )

    if status != "OK":
        raise RuntimeError(
            "Unable to search Blacklist folder."
        )

    uids = (
        data[0].split()
        if data and data[0]
        else []
    )

    added = 0
    deleted = 0

    for uid in uids:
        sender = get_sender(
            mail,
            uid
        )

        if sender:
            if sender not in blacklist:
                blacklist.add(sender)
                added += 1

                print(
                    "Added to blacklist:",
                    sender
                )

            mark_deleted(
                mail,
                uid
            )

            deleted += 1

    if deleted:
        mail.expunge()

    print(
        f"Blacklist folder: "
        f"{added} new sender(s), "
        f"{deleted} message(s) permanently deleted."
    )

    return added


def purge_folder(
    mail,
    folder,
    blacklist
):
    """
    Check recent mail in Inbox / Junk / Trash.
    If sender is already blacklisted:
      \Deleted + EXPUNGE
    """

    if not folder:
        return 0

    print(
        f"Scanning: {folder}"
    )

    select_folder(
        mail,
        folder
    )

    since_date = (
        datetime.utcnow()
        - timedelta(
            days=LOOKBACK_DAYS
        )
    ).strftime("%d-%b-%Y")

    status, data = mail.uid(
        "search",
        None,
        "SINCE",
        since_date
    )

    if status != "OK":
        raise RuntimeError(
            f"Unable to search folder: {folder}"
        )

    uids = (
        data[0].split()
        if data and data[0]
        else []
    )

    deleted = 0

    for uid in uids:
        sender = get_sender(
            mail,
            uid
        )

        if (
            sender
            and sender in blacklist
        ):
            print(
                f"Permanently deleting "
                f"[{folder}]: {sender}"
            )

            mark_deleted(
                mail,
                uid
            )

            deleted += 1

    if deleted:
        # IMAP permanent deletion:
        # messages carrying \Deleted
        # are removed from the selected mailbox.
        mail.expunge()

    print(
        f"{folder}: "
        f"{deleted} blacklisted message(s) deleted."
    )

    return deleted


def main():
    blacklist = load_blacklist()

    print(
        "Loaded blacklist:",
        len(blacklist),
        "sender(s)"
    )

    mail = connect()

    try:
        folders = list_mailboxes(
            mail
        )

        print("\niCloud mailboxes:")

        for item in folders:
            print(
                " -",
                item["name"],
                item["flags"]
            )

        blacklist_folder = (
            ensure_blacklist_folder(
                mail,
                folders
            )
        )

        # Re-read after possibly creating folder.
        folders = list_mailboxes(
            mail
        )

        inbox = find_folder_by_flag(
            folders,
            r"\inbox",
            ["INBOX"]
        )

        if not inbox:
            inbox = "INBOX"

        junk = find_folder_by_flag(
            folders,
            r"\junk",
            [
                "Junk",
                "Junk Email"
            ]
        )

        trash = find_folder_by_flag(
            folders,
            r"\trash",
            [
                "Deleted Messages",
                "Trash",
                "Bin"
            ]
        )

        # First: learn new manually selected
        # senders from Blacklist folder.
        import_blacklist_folder(
            mail,
            blacklist_folder,
            blacklist
        )

        # Save before deleting future mail.
        save_blacklist(
            blacklist
        )

        # Then permanently remove existing
        # blacklisted senders wherever they land.
        purge_folder(
            mail,
            inbox,
            blacklist
        )

        if junk:
            purge_folder(
                mail,
                junk,
                blacklist
            )
        else:
            print(
                "No Junk special-use mailbox found."
            )

        # Also useful because Apple-native blocked
        # senders may already be sent to Trash/Bin.
        if trash:
            purge_folder(
                mail,
                trash,
                blacklist
            )

        print(
            "\nCompleted successfully."
        )

    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
