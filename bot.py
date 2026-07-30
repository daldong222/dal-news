# -*- coding: utf-8 -*-
"""
달동이 뉴스봇
Google News RSS -> 필터 -> 중복제거 -> 텔레그램 전송
GitHub Actions cron으로 자동 실행됩니다.
"""

import os
import re
import json
import time
import html
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta

import requests
import feedparser

import config

# ──────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TOPIC_ID = os.environ.get("TELEGRAM_TOPIC_ID", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SEEN_FILE = "seen.json"
KST = timezone(timedelta(hours=9))

GOOGLE_NEWS = ("https://news.google.com/rss/search"
               "?q={q}&hl=ko&gl=KR&ceid=KR:ko")


# ──────────────────────────────────────────
# 상태 저장 (중복 판정용)
# ──────────────────────────────────────────
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {"first_run": True, "ids": [], "titles": []}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("ids", [])
        data.setdefault("titles", [])
        data["first_run"] = False
        return data
    except Exception:
        return {"first_run": True, "ids": [], "titles": []}


def save_seen(data):
    data["ids"] = data["ids"][-config.SEEN_LIMIT:]
    data["titles"] = data["titles"][-config.SEEN_LIMIT:]
    data.pop("first_run", None)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


# ──────────────────────────────────────────
# 제목 정규화 / 유사도
# ──────────────────────────────────────────
def clean_title(raw):
    """구글뉴스 제목 뒤에 붙는 ' - 매체명' 제거 + 태그 정리"""
    t = html.unescape(raw or "").strip()
    t = re.sub(r"\s+-\s+[^-]{2,20}$", "", t)          # 끝의 " - 한국경제"
    return t.strip()                                   # "[속보]" 등은 살려둠


def norm_key(title):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", title).lower()


def tokens(title):
    """제목을 단어 조각으로 쪼갭니다. '3,900'->3900, '8일째'->8+일째"""
    out = set()
    for tk in re.findall(r"[가-힣]+|[a-z]+|[0-9]+", title.lower()):
        if tk.isdigit():
            out.add(tk.lstrip("0") or "0")
        elif len(tk) >= 2:
            out.add(tk)
    return out


def key_tokens(tok_set):
    """기사의 '주체'가 될 만한 단어만 추립니다.
    (삼성전자·두산에너빌리티·코스피 O / 영업이익·급락·순매수 X)"""
    stop = set(config.DUP_STOPWORDS)
    return {t for t in tok_set
            if not t.isdigit() and t not in stop
            and (len(t) >= 3 or (t.isascii() and len(t) >= 2))}


def overlap(a, b):
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def same_story(t1, t2):
    """
    같은 사건을 다룬 기사인지 판정.
    핵심: '주체 단어가 겹치는가'를 먼저 보고, 그 다음 전체 겹침을 봅니다.
    이 순서를 안 지키면 '삼성전자 영업이익'과 'SK하이닉스 영업이익'을
    같은 기사로 착각합니다.
    """
    A, B = tokens(t1), tokens(t2)
    KA, KB = key_tokens(A), key_tokens(B)
    ov = overlap(A, B)
    if KA and KB:
        return bool(KA & KB) and ov >= config.DUP_OVERLAP
    return ov >= config.DUP_OVERLAP_LOOSE       # 주체를 못 잡은 경우


def is_duplicate(title, seen_titles):
    if len(norm_key(title)) < 6:
        return True
    for old in reversed(seen_titles[-300:]):
        if same_story(title, old):
            return True
    return False


# ──────────────────────────────────────────
# 수집
# ──────────────────────────────────────────
def feed_urls():
    urls = []
    for kw in config.KEYWORDS:
        q = urllib.parse.quote(f"{kw} when:1d")
        urls.append((kw, GOOGLE_NEWS.format(q=q)))
    for u in config.EXTRA_FEEDS:
        urls.append(("직접구독", u))
    return urls


def entry_time(entry):
    for field in ("published_parsed", "updated_parsed"):
        tm = entry.get(field)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc)
    return None


def source_of(entry):
    src = entry.get("source")
    if isinstance(src, dict) and src.get("title"):
        return src["title"]
    raw = html.unescape(entry.get("title", ""))
    m = re.search(r"\s+-\s+([^-]{2,20})$", raw)
    return m.group(1).strip() if m else ""


def collect():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config.MAX_AGE_HOURS)
    items = []

    for kw, url in feed_urls():
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[경고] 피드 실패 {kw}: {e}")
            continue

        for e in parsed.entries[:15]:
            title = clean_title(e.get("title", ""))
            link = e.get("link", "")
            if not title or not link:
                continue

            if any(b in title for b in config.BLOCKLIST):
                continue

            src = source_of(e)
            if src and any(bs in src for bs in getattr(config, "BLOCK_SOURCES", [])):
                continue

            pub = entry_time(e)
            if pub and pub < cutoff:
                continue

            uid = hashlib.md5(norm_key(title).encode()).hexdigest()[:16]
            items.append({
                "uid": uid,
                "title": title,
                "link": link,
                "source": src,
                "keyword": kw,
                "pub": pub or now,
            })

    items.sort(key=lambda x: x["pub"], reverse=True)
    print(f"[수집] 원본 {len(items)}건")
    return items


# ──────────────────────────────────────────
# 한 줄 코멘트
# ──────────────────────────────────────────
COMMENT_SYSTEM = """당신은 한국 주식·경제 콘텐츠 채널의 리서처입니다.
기사 제목을 받아서, 각 제목마다 한 줄 코멘트를 씁니다.

목적: 기사 문장을 그대로 옮기지 않고, "이게 왜 볼 만한가"를 내 말로 한 줄 얹는 것.

규칙:
1. 정확히 한 문장. {max_chars}자 이내. 다나까체(~습니다/~입니다)로 끝낸다.
2. 제목에 담긴 정보만 쓴다. 제목에 없는 숫자·날짜·회사명·인용을 절대 만들어내지 않는다.
   제목 정보가 부족하면 사실을 보태지 말고, 그 사안이 어느 지점에서 중요해지는지만 짚는다.
3. 방향성 판단 금지. 오른다/내린다/매수/매도/저평가/기회/주목할 종목 같은 표현을 쓰지 않는다.
   투자 판단은 읽는 사람이 한다.
4. 기사 제목을 다른 말로 바꿔 되풀이하지 않는다. 요약이 아니라 관점을 한 줄 얹는 것이다.
   "무슨 일이 있었다"가 아니라 "그래서 어디를 봐야 하는 사안이다" 쪽으로 쓴다.
5. 단정하지 않는다. 확인되지 않은 인과는 "~로 보입니다", "~인지가 관건입니다"처럼 여백을 둔다.
6. 과장·낚시 표현("충격", "역대급", "폭탄") 금지. 담백하게 쓴다.

출력: JSON 배열만. 설명·머리말·코드블록 없이.
["첫 번째 코멘트", "두 번째 코멘트", ...]
입력 제목 개수와 배열 길이가 정확히 같아야 한다."""


def make_comments(items):
    """제목 목록 -> 한 줄 코멘트 목록. 실패하면 빈 dict (코멘트 없이 발송)."""
    if not getattr(config, "COMMENT_ON", False) or not ANTHROPIC_KEY or not items:
        return {}

    listing = "\n".join(f"{i + 1}. {it['title']}" for i, it in enumerate(items))
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": config.COMMENT_MODEL,
                "max_tokens": 1000,
                "system": COMMENT_SYSTEM.format(
                    max_chars=config.COMMENT_MAX_CHARS),
                "messages": [{"role": "user", "content": listing}],
            },
            timeout=60,
        )
        if not r.ok:
            print(f"[코멘트 실패] {r.status_code} {r.text[:200]}")
            return {}

        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        arr = json.loads(text[text.index("["):text.rindex("]") + 1])

        if len(arr) != len(items):
            print(f"[코멘트 경고] 개수 불일치 {len(arr)} vs {len(items)} — 코멘트 생략")
            return {}

        out = {}
        for it, c in zip(items, arr):
            c = re.sub(r"\s+", " ", str(c)).strip()
            if c:
                out[it["uid"]] = c
        return out

    except Exception as e:
        print(f"[코멘트 실패] {e}")
        return {}


def emoji_for(title):
    for words, mark in getattr(config, "EMOJI_RULES", []):
        if any(w in title for w in words):
            return mark
    return getattr(config, "EMOJI_DEFAULT", "📰")


# ──────────────────────────────────────────
# 전송
# ──────────────────────────────────────────
def esc(s):
    return html.escape(s, quote=False)


def build_message(it, comment=None):
    when = it["pub"].astimezone(KST).strftime("%H:%M")
    lines = [f"{emoji_for(it['title'])} <b>{esc(it['title'])}</b>"]

    if comment:
        lines.append(f"{config.COMMENT_PREFIX} {esc(comment)}")

    meta = f"#{esc(it['keyword'].replace(' ', '_'))}"
    if it["source"]:
        meta += f" · {esc(it['source'])}"
    meta += f" · {when}"
    footer = getattr(config, "FOOTER", "")
    if footer:
        meta += f" {esc(footer)}"

    lines.append("")
    lines.append(meta)
    lines.append(it["link"])
    return "\n".join(lines)


def send(text):
    if not TOKEN or not CHAT_ID:
        print("[건너뜀] 토큰/챗ID 없음 (드라이런)")
        print(text, "\n---")
        return True
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if TOPIC_ID:
        try:
            payload["message_thread_id"] = int(TOPIC_ID)
        except ValueError:
            print(f"[경고] TELEGRAM_TOPIC_ID가 숫자가 아닙니다: {TOPIC_ID}")

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 30)
            print(f"[대기] 429 — {wait}초")
            time.sleep(wait + 1)
            return send(text)
        if not r.ok:
            body = r.text[:200]
            if "thread not found" in body:
                print("[실패] 주제방 번호가 틀렸습니다. "
                      "TELEGRAM_TOPIC_ID를 확인하거나 비워두세요.")
            elif "chat not found" in body:
                print("[실패] chat_id가 틀렸거나 봇이 그룹에 없습니다.")
            elif "not enough rights" in body:
                print("[실패] 봇 권한이 부족합니다. 관리자로 승격했는지 확인하세요.")
            else:
                print(f"[실패] {r.status_code} {body}")
            return False
        return True
    except Exception as e:
        print(f"[실패] 전송 예외: {e}")
        return False


# ──────────────────────────────────────────
def in_quiet_hours():
    """새벽 시간대면 True. 자정을 넘는 구간도 처리."""
    q = getattr(config, "QUIET_HOURS", None)
    if not q:
        return False
    start, end = q
    h = datetime.now(KST).hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end          # 예: (23, 7)


def main():
    if in_quiet_hours():
        print(f"[침묵] 조용 시간 {config.QUIET_HOURS} — 전송 건너뜀")
        return

    seen = load_seen()
    items = collect()

    fresh = []
    for it in items:
        if it["uid"] in seen["ids"]:
            continue
        if is_duplicate(it["title"], seen["titles"]):
            continue
        fresh.append(it)
        seen["ids"].append(it["uid"])
        seen["titles"].append(it["title"])

    if seen.get("first_run"):
        # 첫 실행은 과거 기사 전부 쏟아내면 도배 → 최신 2건만
        print("[첫 실행] 기존 기사는 읽음 처리, 최신 2건만 발송")
        fresh = fresh[:2]

    to_send = fresh[:config.MAX_PER_RUN]
    print(f"[발송] 신규 {len(fresh)}건 중 {len(to_send)}건 전송")

    comments = make_comments(to_send)
    if getattr(config, "COMMENT_ON", False):
        print(f"[코멘트] {len(comments)}/{len(to_send)}건 생성")

    sent = 0
    for it in to_send:
        if send(build_message(it, comments.get(it["uid"]))):
            sent += 1
        time.sleep(config.SEND_DELAY_SEC)

    save_seen(seen)
    print(f"[완료] {sent}건 전송, 창고 {len(seen['ids'])}건")


if __name__ == "__main__":
    main()
