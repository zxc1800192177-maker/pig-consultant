"""一次性的帳號維護工具:列出帳號、重設密碼、刪除帳號。

**為什麼需要它**:App 內的「刪除帳號」與登入都要求先有密碼。密碼弄丟時
那個帳號就完全碰不到了 —— 既登不進去,也刪不掉,連名稱都放不出來。
這是實際發生過的情況。

**為什麼不做成網頁上的功能**:那等於在正式站開一個「不需要密碼就能刪別人
帳號」的入口,一旦守衛寫錯就是災難性的漏洞,而這個 App 是要給其他牧場用的。
做成需要資料庫連線字串才能跑的離線工具,鑰匙就只在牧場主自己手上,
正式站上不留任何後門。

用法(在自己的電腦上跑,不是在伺服器上):

    pip install "psycopg[binary]"
    python tools/admin_account.py list
    python tools/admin_account.py reset  <使用者名稱>
    python tools/admin_account.py delete <使用者名稱>

連線字串從 Render 後台的 PostgreSQL 頁面複製(External Database URL),
用環境變數傳進來,不要打在指令裡 —— 打在指令裡會留在終端機歷史紀錄。

    Windows PowerShell:  $env:DATABASE_URL = "postgresql://..."
    macOS / Linux:       export DATABASE_URL="postgresql://..."
"""

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from auth import hash_password, validate_password  # noqa: E402
from db import PostgresStore  # noqa: E402


def _store() -> PostgresStore:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        sys.exit("沒有設定 DATABASE_URL。見這個檔案開頭的說明。")
    return PostgresStore(dsn)


def _find(store: PostgresStore, username: str) -> dict:
    row = store.get_user_by_username(username)
    if row is None:
        sys.exit(f"找不到使用者「{username}」。先用 list 看看確切的名稱長什麼樣。")
    return row


def _describe(store: PostgresStore, row: dict) -> str:
    """刪之前先講清楚會連帶失去什麼,不要讓人憑帳號名稱猜。"""
    farm_id = row.get("farm_id")
    if farm_id is None:
        return "這個帳號沒有對應的牧場"
    sows = store.list_sows(farm_id)
    events = store.list_sow_events(farm_id)
    boars = store.list_boars(farm_id)
    return (f"牧場 #{farm_id}:{len(sows)} 頭母豬、{len(boars)} 頭公豬、"
            f"{len(events)} 筆母豬事件")


def cmd_list(store: PostgresStore, _args) -> None:
    """列出所有正式帳號。

    **只印名稱**,不印密碼雜湊 —— 印出來的東西會留在終端機歷史紀錄裡。
    名稱旁邊用引號框住,才看得出有沒有多餘的空白或看不見的字元
    (「登不進去」有時就是這種原因)。
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, username, farm_id, is_guest FROM users"
            " WHERE username IS NOT NULL ORDER BY id"
        ).fetchall()
    if not rows:
        print("沒有任何正式帳號。")
        return
    print(f"{len(rows)} 個帳號:")
    for user_id, username, farm_id, is_guest in rows:
        tag = " (訪客)" if is_guest else ""
        print(f"  #{user_id}  「{username}」  牧場={farm_id}{tag}")


def cmd_reset(store: PostgresStore, args) -> None:
    """重設密碼。**比刪除溫和得多,而且資料與名稱都留著。**"""
    row = _find(store, args.username)
    print(f"帳號 #{row['id']}「{row['username']}」 — {_describe(store, row)}")

    new = getpass.getpass("新密碼(不會顯示):")
    if new != getpass.getpass("再輸入一次:"):
        sys.exit("兩次輸入不一樣,沒有更動。")
    try:
        validate_password(new, username=row["username"])
    except Exception as e:                      # ValidationError
        sys.exit(f"密碼不符合規則:{e}")

    with store._connect() as conn:
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                     (hash_password(new), row["id"]))
        # 一併把既有的登入狀態清掉(OWASP:重設密碼後要讓既有 session 失效)。
        # 密碼會被重設,常常正是因為擔心帳號被別人拿走 —— 只改密碼而讓對方
        # 原本那張 cookie 繼續有效的話,等於沒有把人請出去。
        killed = conn.execute(
            "DELETE FROM sessions WHERE user_id = %s RETURNING token", (row["id"],)
        ).fetchall()

    print(f"密碼已重設,資料完全沒有動到。")
    if killed:
        print(f"順帶登出了 {len(killed)} 個既有的登入狀態,請重新登入。")


def cmd_delete(store: PostgresStore, args) -> None:
    """刪除帳號,連同他獨有的牧場資料。無法復原。"""
    row = _find(store, args.username)
    print(f"帳號 #{row['id']}「{row['username']}」 — {_describe(store, row)}")
    print("\n這個動作無法復原,而且沒有備份可以救回。")
    print("(想保留資料的話,改用 reset 重設密碼就能登入了。)")

    # 要求把名稱完整打一次,而不是按 y —— 按 y 太容易在沒看清楚時按下去。
    if input("確定要刪除的話,完整輸入帳號名稱:") != row["username"]:
        sys.exit("名稱不符,沒有刪除任何東西。")

    store.delete_account(row["id"])
    print(f"已刪除。「{row['username']}」這個名稱現在可以重新註冊了。")


def main() -> None:
    parser = argparse.ArgumentParser(description="帳號維護工具(離線執行)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有帳號")
    for name, help_text in (("reset", "重設密碼(保留資料)"),
                            ("delete", "刪除帳號(無法復原)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("username")

    args = parser.parse_args()
    store = _store()
    {"list": cmd_list, "reset": cmd_reset, "delete": cmd_delete}[args.command](store, args)


if __name__ == "__main__":
    main()
