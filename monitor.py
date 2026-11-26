from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup


# ==================================================
# 基本設定
# ==================================================

SOURCE_URL = "https://cp.toyota.jp/rentacar/"
LINE_API_URL = "https://api.line.me/v2/bot/message/push"

STATE_PATH = Path("state.json")

MAX_FETCH_RETRIES = 3
MAX_LINE_RETRIES = 3

REQUEST_TIMEOUT = 30

PERIOD_RE = re.compile(
    r"\d{4}年\s*\d{1,2}月\s*\d{1,2}日\s*[～〜~-]\s*"
    r"(?:\d{4}年\s*)?\d{1,2}月\s*\d{1,2}日"
)

PHONE_RE = re.compile(
    r"0\d{1,4}-\d{1,4}-\d{3,4}"
)


# ==================================================
# ログ
# ==================================================

def log(message: str) -> None:
    now = datetime.now(timezone.utc).astimezone()

    print(
        f"[{now.strftime('%Y-%m-%d %H:%M:%S %Z')}] "
        f"{message}",
        flush=True
    )


# ==================================================
# 文字列処理
# ==================================================

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def find_label(
    tokens: list[str],
    start: int,
    labels: set[str],
    limit: int = 20
) -> int | None:

    end = min(len(tokens), start + limit)

    for i in range(start, end):
        if compact(tokens[i]) in labels:
            return i

    return None


def find_matching(
    tokens: list[str],
    start: int,
    pattern: re.Pattern[str],
    limit: int = 20
) -> int | None:

    end = min(len(tokens), start + limit)

    for i in range(start, end):
        if pattern.search(tokens[i]):
            return i

    return None


# ==================================================
# 掲載ID生成
# ==================================================

def listing_id(item: dict[str, str]) -> str:

    stable = "\n".join([
        item["start"],
        item["return"],
        item["period"],
        item["car"]
    ])

    return hashlib.sha256(
        stable.encode("utf-8")
    ).hexdigest()[:24]


# ==================================================
# 掲載情報解析
# ==================================================

def extract_listings(
    html: bytes
) -> list[dict[str, str]]:

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in soup([
        "script",
        "style",
        "noscript",
        "template"
    ]):
        tag.decompose()

    tokens = [
        clean(x)
        for x in soup.stripped_strings
        if clean(x)
    ]

    results: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    i = 0

    while i < len(tokens):

        token = compact(tokens[i])

        if token == "出発":

            if (
                i + 1 >= len(tokens)
                or compact(tokens[i + 1]) != "店舗"
            ):
                i += 1
                continue

            start_pos = i + 2

        elif token == "出発店舗":

            start_pos = i + 1

        else:

            i += 1
            continue

        return_label = find_label(
            tokens,
            start_pos,
            {"返却", "返却店舗"},
            limit=15
        )

        if (
            return_label is None
            or return_label == start_pos
        ):
            i += 1
            continue

        start_store = clean(
            " ".join(tokens[start_pos:return_label])
        )

        return_pos = return_label + 1

        if compact(tokens[return_label]) == "返却":

            if (
                return_pos < len(tokens)
                and compact(tokens[return_pos]) == "店舗"
            ):
                return_pos += 1

        period_label = find_label(
            tokens,
            return_pos,
            {"出発期間"},
            limit=15
        )

        if (
            period_label is None
            or period_label == return_pos
        ):
            i += 1
            continue

        return_store = clean(
            " ".join(tokens[return_pos:period_label])
        )

        period_idx = find_matching(
            tokens,
            period_label + 1,
            PERIOD_RE,
            limit=8
        )

        if period_idx is None:
            i += 1
            continue

        period_match = PERIOD_RE.search(
            tokens[period_idx]
        )

        assert period_match is not None

        period = clean(
            period_match.group(0)
        )

        car_label = find_label(
            tokens,
            period_idx + 1,
            {"車種"},
            limit=20
        )

        if car_label is None:
            i += 1
            continue

        condition_label = find_label(
            tokens,
            car_label + 1,
            {"車両条件"},
            limit=12
        )

        if (
            condition_label is None
            or condition_label == car_label + 1
        ):
            i += 1
            continue

        car = clean(
            " ".join(
                tokens[car_label + 1:condition_label]
            )
        )

        phone_label = find_label(
            tokens,
            condition_label + 1,
            {"予約電話番号"},
            limit=20
        )

        if phone_label is None:
            i += 1
            continue

        condition_parts = []

        for x in tokens[
            condition_label + 1:phone_label
        ]:

            if PERIOD_RE.search(x):
                continue

            condition_parts.append(x)

        conditions = clean(
            " ".join(condition_parts)
        )

        phone_idx = find_matching(
            tokens,
            phone_label + 1,
            PHONE_RE,
            limit=10
        )

        if phone_idx is None:
            i += 1
            continue

        phone_match = PHONE_RE.search(
            tokens[phone_idx]
        )

        assert phone_match is not None

        phone = phone_match.group(0)

        contact = clean(
            " ".join(
                tokens[phone_label + 1:phone_idx]
            )
        )

        if not all([
            start_store,
            return_store,
            period,
            car,
            phone
        ]):
            i += 1
            continue

        item = {
            "start": start_store,
            "return": return_store,
            "period": period,
            "car": car,
            "conditions": conditions or "記載なし",
            "contact": contact or "記載なし",
            "phone": phone
        }

        item["id"] = listing_id(item)

        if item["id"] not in seen_ids:

            seen_ids.add(item["id"])
            results.append(item)

        i = phone_idx + 1

    return results


# ==================================================
# 絞り込み
# ==================================================

def parse_keywords(name: str) -> list[str]:

    raw = os.getenv(name, "").strip()

    return [
        x.strip()
        for x in raw.split(",")
        if x.strip()
    ]


def includes_any(
    text: str,
    keywords: Iterable[str]
) -> bool:

    words = list(keywords)

    if not words:
        return True

    folded = text.casefold()

    return any(
        word.casefold() in folded
        for word in words
    )


def apply_filters(
    items: list[dict[str, str]]
) -> list[dict[str, str]]:

    start_words = parse_keywords("FILTER_START")
    return_words = parse_keywords("FILTER_RETURN")
    car_words = parse_keywords("FILTER_CAR")

    log(f"出発店舗フィルター: {start_words}")
    log(f"返却店舗フィルター: {return_words}")
    log(f"車種フィルター: {car_words}")

    return [
        item
        for item in items

        if includes_any(
            item["start"],
            start_words
        )

        and includes_any(
            item["return"],
            return_words
        )

        and includes_any(
            item["car"],
            car_words
        )
    ]


# ==================================================
# Webページ取得
# ==================================================

def fetch_page() -> bytes:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(compatible; KatamichiGoMonitor/2.0; "
            "+https://github.com/)"
        )
    }

    last_error = None

    for attempt in range(MAX_FETCH_RETRIES):

        try:

            log(
                f"Webページ取得 "
                f"({attempt + 1}/{MAX_FETCH_RETRIES})"
            )

            response = requests.get(
                SOURCE_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            log(
                f"[FETCH] HTTP Status: "
                f"{response.status_code}"
            )

            response.raise_for_status()

            if not response.content:
                raise RuntimeError(
                    "取得したHTMLが空です。"
                )

            return response.content

        except requests.RequestException as exc:

            last_error = exc

            log(f"[FETCH ERROR] {exc}")

            if attempt < MAX_FETCH_RETRIES - 1:

                wait = 2 ** attempt

                log(f"{wait}秒後に再試行します。")

                time.sleep(wait)

    raise RuntimeError(
        f"Webページの取得に失敗しました: {last_error}"
    )


# ==================================================
# state.json読み込み
# ==================================================

def load_state() -> dict:

    if not STATE_PATH.exists():

        log("state.jsonが存在しません。")

        return {
            "initialized": False,
            "current_ids": []
        }

    try:

        data = json.loads(
            STATE_PATH.read_text(
                encoding="utf-8"
            )
        )

    except (json.JSONDecodeError, OSError) as exc:

        raise RuntimeError(
            f"state.jsonを読み込めません: {exc}"
        )

    if not isinstance(data, dict):

        raise RuntimeError(
            "state.jsonの形式が不正です。"
        )

    if not isinstance(
        data.get("initialized"),
        bool
    ):

        raise RuntimeError(
            "state.jsonのinitializedが不正です。"
        )

    ids = data.get("current_ids")

    if (
        not isinstance(ids, list)
        or not all(
            isinstance(x, str)
            for x in ids
        )
    ):

        raise RuntimeError(
            "state.jsonのcurrent_idsが不正です。"
        )

    log(
        f"state.json読み込み完了: {len(ids)}件"
    )

    return data


# ==================================================
# state.json保存
# ==================================================

def save_state_ids(
    ids: set[str] | list[str],
    initialized: bool = True
) -> None:

    data = {
        "initialized": initialized,
        "current_ids": sorted(set(ids))
    }

    temp_path = STATE_PATH.with_name(
        "state.json.tmp"
    )

    temp_path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    os.replace(
        temp_path,
        STATE_PATH
    )

    log(
        f"state.json保存完了: "
        f"{len(data['current_ids'])}件"
    )


def save_state(
    items: list[dict[str, str]]
) -> None:

    ids = {
        item["id"]
        for item in items
    }

    save_state_ids(ids)


# ==================================================
# LINE通知
# ==================================================

def send_line(message: str) -> None:

    token = os.getenv(
        "LINE_CHANNEL_ACCESS_TOKEN",
        ""
    ).strip()

    user_id = os.getenv(
        "LINE_USER_ID",
        ""
    ).strip()

    if not token:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKENが設定されていません。"
        )

    if not user_id:
        raise RuntimeError(
            "LINE_USER_IDが設定されていません。"
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}"
    }

    payload = {
        "to": user_id,
        "messages": [
            {
                "type": "text",
                "text": message[:5000]
            }
        ]
    }

    for attempt in range(MAX_LINE_RETRIES):

        try:

            log(
                f"[LINE] 送信 "
                f"({attempt + 1}/{MAX_LINE_RETRIES})"
            )

            response = requests.post(
                LINE_API_URL,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )

            log(
                f"[LINE] HTTP Status: "
                f"{response.status_code}"
            )

            if response.status_code == 200:

                log("[LINE] 通知成功")

                return

            log(
                f"[LINE] Response: "
                f"{response.text[:1000]}"
            )

            # 429や5xxは再試行する
            if (
                response.status_code == 429
                or response.status_code >= 500
            ):

                if attempt < MAX_LINE_RETRIES - 1:

                    time.sleep(2 ** attempt)
                    continue

            response.raise_for_status()

        except requests.RequestException as exc:

            log(f"[LINE ERROR] {exc}")

            if attempt < MAX_LINE_RETRIES - 1:

                time.sleep(2 ** attempt)

            else:

                raise

    raise RuntimeError(
        "LINE通知に失敗しました。"
    )


# ==================================================
# 初回起動通知
# ==================================================

def notify_started(count: int) -> None:

    message = (
        "トヨタ 片道GO! の監視を開始しました。\n\n"
        f"現在の条件に一致する掲載：{count}件\n\n"
        "次回以降、新しく追加された掲載を"
        "LINEに通知します。"
    )

    send_line(message)


# ==================================================
# 新着掲載通知
# ==================================================

def notify_listing(
    item: dict[str, str]
) -> None:

    message = (
        "トヨタ 片道GO! 新着掲載\n\n"

        "━━━━━━━━━━━━━━\n\n"

        f"出発店舗\n{item['start']}\n\n"

        f"返却店舗\n{item['return']}\n\n"

        f"車種\n{item['car']}\n\n"

        f"出発期間\n{item['period']}\n\n"

        f"車両条件\n{item['conditions']}\n\n"

        f"予約連絡先\n{item['contact']}\n"

        f"電話番号\n{item['phone']}\n\n"

        "━━━━━━━━━━━━━━\n\n"

        "公式サイト\n"
        "https://cp.toyota.jp/rentacar/"
    )

    send_line(message)


# ==================================================
# メイン処理
# ==================================================

def main() -> None:

    log("====================================")
    log("Toyota Katamichi GO Monitor Start")
    log("====================================")

    # 1. Webページ取得
    html = fetch_page()

    # 2. 掲載情報解析
    all_items = extract_listings(html)

    log(
        f"掲載情報解析完了: "
        f"{len(all_items)}件"
    )

    if not all_items:

        raise RuntimeError(
            "掲載情報を1件も解析できませんでした。"
            "Webページの構造が変更された可能性があります。"
        )

    # 3. 絞り込み
    items = apply_filters(all_items)

    log(
        f"条件一致: {len(items)}件"
    )

    # 4. 状態読み込み
    state = load_state()

    old_ids = set(
        state["current_ids"]
    )

    current_ids = {
        item["id"]
        for item in items
    }

    # 5. 初回起動
    if not state["initialized"]:

        log("初回起動を検出しました。")

        notify_started(
            len(items)
        )

        save_state(items)

        log("初回登録完了")

        return

    # 6. 新着判定
    new_items = [
        item
        for item in items
        if item["id"] not in old_ids
    ]

    log(
        f"取得件数: {len(all_items)}件 / "
        f"条件一致: {len(items)}件 / "
        f"新着: {len(new_items)}件"
    )

    # 7. 新着通知
    sent_ids = set(old_ids)

    for index, item in enumerate(new_items, start=1):

        log(
            f"新着通知 {index}/{len(new_items)}: "
            f"{item['start']} → {item['return']}"
        )

        notify_listing(item)

        # 送信成功したIDだけを追加
        sent_ids.add(item["id"])

        # 1件ごとに保存する
        # 途中で失敗しても成功済み通知を再送しない
        save_state_ids(sent_ids)

        time.sleep(0.4)

    # 8. 現在掲載中のIDに更新
    if sent_ids != current_ids:

        save_state_ids(current_ids)

    log("監視処理正常終了")

    log("====================================")


# ==================================================
# 実行
# ==================================================

if __name__ == "__main__":

    try:

        main()

    except Exception as exc:

        log(
            f"[FATAL ERROR] "
            f"{type(exc).__name__}: {exc}"
        )

        raise
