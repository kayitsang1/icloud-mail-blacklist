import imaplib
import os
import re
from pathlib import Path
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr


LOOKBACK_DAYS = 7
BLACKLIST_FOLDER = "Blacklist"


ACCOUNTS = [
    {
        "name": "iCloud",
        "host": "imap.mail.me.com",
        "port": 993,
        "email_env": "ICLOUD_EMAIL",
        "password_env": "ICLOUD_APP_PASSWORD",
        "blacklist_file": "icloud_blacklist.txt",
    },
    {
        "name": "Yahoo",
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "email_env": "YAHOO_EMAIL",
        "password_env": "YAHOO_APP_PASSWORD",
        "blacklist_file": "yahoo_blacklist.txt",
    },
    {
        "name": "QQ",
        "host": "imap.qq.com",
        "port": 993,
        "email_env": "QQ_EMAIL",
        "password_env": "QQ_AUTH_CODE",
        "blacklist_file": "qq_blacklist.txt",
    },
]


def normalize_email(address):
    return str(address or "").strip().lower()


def load_blacklist(path):
    file = Path(path)

    if not file.exists():
        return set()

    return {
        normalize_email(line)
        for line in file.read_text(
            encoding="utf-8"
        ).splitlines()
        if normalize_email(line)
    }


def save_blacklist(path, blacklist):
    file = Path(path)

    content = "\n".join(
        sorted(blacklist)
    )

    if content:
        content += "\n"

    file.write_text(
        content,
        encoding="utf-8"
    )


def connect(account):
    email = os.environ.get(
        account["email_env"],
        ""
    ).strip()

    password = os.environ.get(
        account["password_env"],
        ""
    ).strip()

    if not email or not password:
        print(
            f"[{account['name']}] "
            "Credentials missing. Skipping."
        )
        return None

    print(
        f"[{account['name']}] "
        f"Connecting to {account['host']}..."
    )

    mail = imaplib.IMAP4_SSL(
        account["host"],
        account["port"]
    )

    mail.login(
        email,
        password
    )

    print(
        f"[{account['name']}] "
        "IMAP login successful."
    )

    return mail


def list_mailboxes(mail):
    status, data = mail.list()

    if status != "OK":
        raise RuntimeError(
            "Unable to list mailboxes."
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
        if flag in folder["flags"].lower():
            return folder["name"]

    for wanted in fallback_names or []:
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
        "Creating..."
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


def get_sender(
    mail,
    uid
):
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
            and isinstance(item[1], bytes)
        ):
            raw_header += item[1]

    if not raw_header:
        return ""

    message = BytesParser(
        policy=policy.default
    ).parsebytes(
        raw_header
    )

    sender = parseaddr(
        message.get("From", "")
    )[1]

    return normalize_email(
        sender
    )


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
            f"Unable to delete message UID {uid}"
        )


def import_blacklist_folder(
    mail,
    folder,
    blacklist,
    account_name
):
    print(
        f"[{account_name}] "
        f"Scanning Blacklist folder..."
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

        if not sender:
            continue

        if sender not in blacklist:
            blacklist.add(
                sender
            )

            added += 1

            print(
                f"[{account_name}] "
                f"Added: {sender}"
            )

        mark_deleted(
            mail,
            uid
        )

        deleted += 1

    if deleted:
        mail.expunge()

    print(
        f"[{account_name}] "
        f"Blacklist folder: "
        f"{added} new sender(s), "
        f"{deleted} message(s) deleted."
    )


def purge_folder(
    mail,
    folder,
    blacklist,
    account_name
):
    if not folder:
        return 0

    print(
        f"[{account_name}] "
        f"Scanning {folder}..."
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
    ).strftime(
        "%d-%b-%Y"
    )

    status, data = mail.uid(
        "search",
        None,
        "SINCE",
        since_date
    )

    if status != "OK":
        raise RuntimeError(
            f"Unable to search {folder}"
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
                f"[{account_name}] "
                f"Deleting [{folder}]: "
                f"{sender}"
            )

            mark_deleted(
                mail,
                uid
            )

            deleted += 1

    if deleted:
        mail.expunge()

    print(
        f"[{account_name}] "
        f"{folder}: "
        f"{deleted} message(s) deleted."
    )

    return deleted


def process_account(
    account
):
    blacklist = load_blacklist(
        account["blacklist_file"]
    )

    print(
        f"\n========== {account['name']} =========="
    )

    print(
        f"[{account['name']}] "
        f"Loaded blacklist: "
        f"{len(blacklist)} sender(s)"
    )

    mail = None

    try:
        mail = connect(
            account
        )

        if mail is None:
            return

        folders = list_mailboxes(
            mail
        )

        print(
            f"[{account['name']}] Mailboxes:"
        )

        for folder in folders:
            print(
                " -",
                folder["name"],
                folder["flags"]
            )

        blacklist_folder = (
            ensure_blacklist_folder(
                mail,
                folders
            )
        )

        folders = list_mailboxes(
            mail
        )

        inbox = (
            find_folder_by_flag(
                folders,
                r"\inbox",
                [
                    "INBOX",
                    "Inbox"
                ]
            )
            or "INBOX"
        )

        junk = find_folder_by_flag(
            folders,
            r"\junk",
            [
                "Junk",
                "Junk Email",
                "Spam",
                "Bulk Mail"
            ]
        )

        trash = find_folder_by_flag(
            folders,
            r"\trash",
            [
                "Trash",
                "Bin",
                "Deleted",
                "Deleted Messages"
            ]
        )

        import_blacklist_folder(
            mail,
            blacklist_folder,
            blacklist,
            account["name"]
        )

        save_blacklist(
            account["blacklist_file"],
            blacklist
        )

        purge_folder(
            mail,
            inbox,
            blacklist,
            account["name"]
        )

        if junk:
            purge_folder(
                mail,
                junk,
                blacklist,
                account["name"]
            )
        else:
            print(
                f"[{account['name']}] "
                "Junk/Spam folder not detected."
            )

        if trash:
            purge_folder(
                mail,
                trash,
                blacklist,
                account["name"]
            )

        print(
            f"[{account['name']}] "
            "Completed successfully."
        )

    except Exception as exc:
        # One account failing must not
        # prevent the others being processed.
        print(
            f"[{account['name']}] ERROR:"
        )
        print(
            repr(exc)
        )

    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


def main():
    for account in ACCOUNTS:
        process_account(
            account
        )


if __name__ == "__main__":
    main()
