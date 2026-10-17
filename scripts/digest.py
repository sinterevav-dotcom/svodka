"""
Сводка — сбор новостей, пересказ через Gemini и отправка в Telegram.

Запускается по расписанию в GitHub Actions. Переменные окружения:
  GEMINI_API_KEY      — ключ Google AI Studio (если пусто — бот пришлёт просто заголовки)
  GEMINI_MODEL        — модель Gemini (по умолчанию gemini-2.5-flash)
  TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
  TELEGRAM_CHAT_ID    — куда слать: @имя_канала или числовой id; можно несколько через запятую
  MINIAPP_URL         — ссылка, которую откроет кнопка «Открыть сводку»
  DRY_RUN=1           — ничего не отправлять, только собрать digest.json
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
FEEDS_FILE = ROOT / "feeds.json"
OUT_FILE = ROOT / "docs" / "data" / "digest.json"

TZ = timezone(timedelta(hours=7))  # Новосибирск
RUBRICS = ["Бизнес", "Россия", "Мир", "Идеи"]
MAX_PER_FEED = 15
MAX_ITEMS_TO_MODEL = 80
TOP_N = 12
HOURS_BACK = 18

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def find_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for l in entry.get("links", []) or []:
        if l.get("type", "").startswith("image/") and l.get("href"):
            return l["href"]
    for e in entry.get("enclosures", []) or []:
        if e.get("type", "").startswith("image/") and e.get("href"):
            return e["href"]
    m = re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "") or "")
    return m.group(1) if m else None


OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.I)


def og_image(url):
    """Обложка статьи из её страницы (og:image), если в RSS картинки не было."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 SvodkaBot"})
        m = OG_RE.search(r.text[:200000])
        if m:
            img = html.unescape(m.group(1) or m.group(2))
            return requests.compat.urljoin(r.url, img)
    except Exception as e:
        log(f"[og] {url[:60]}: {e}")
    return None


def fill_images(digest):
    for it in digest["items"]:
        if not it.get("image"):
            for s in it["sources"]:
                img = og_image(s["url"])
                if img:
                    it["image"] = img
                    break


def collect():
    feeds = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    items, seen = [], set()
    for f in feeds:
        try:
            resp = requests.get(f["url"], timeout=20, headers={"User-Agent": "Mozilla/5.0 SvodkaBot"})
            parsed = feedparser.parse(resp.content)
        except Exception as e:  # один упавший источник не ломает сводку
            log(f"[feed] {f['name']}: {e}")
            continue
        count = 0
        for entry in parsed.entries:
            if count >= MAX_PER_FEED:
                break
            title = clean(entry.get("title"), 200)
            link = entry.get("link")
            if not title or not link:
                continue
            key = re.sub(r"\W+", "", title.lower())[:60]
            if key in seen:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime.fromtimestamp(time.mktime(ts), timezone.utc) if ts else None
            if published and published < cutoff:
                continue
            seen.add(key)
            items.append({
                "id": f"n{len(items)}",
                "source": f["name"],
                "rubric": f["rubric"],
                "title": title,
                "summary": clean(entry.get("summary")),
                "url": link,
                "image": find_image(entry),
                "published": published.isoformat() if published else None,
            })
            count += 1
        log(f"[feed] {f['name']}: {count}")
    return items


PROMPT = """Ты — редактор утренней деловой сводки для предпринимателя из России.
Ниже список новостей за последние часы (JSON). Отбери самые важные для бизнеса —
до {top} штук, убери дубли (одно событие из разных источников объединяй и укажи все id).

Для каждой отобранной новости верни:
- ids: список id исходных новостей
- rubric: одно из {rubrics}
- title: короткий заголовок на русском, до 90 символов, без кликбейта
- bullets: 2–3 коротких пункта на русском — только факты из текста новостей
- impact: одно предложение «что это значит для бизнеса в России», без выдумок
- importance: число от 1 до 10

Также верни:
- intro: одно предложение — главное за день
- key_figures: до 3 ключевых цифр, которые ЯВНО есть в новостях: {{"label","value","note"}}

Не добавляй фактов, которых нет в новостях. Ответ — строго JSON:
{{"intro": "...", "key_figures": [...], "items": [...]}}

Новости:
{news}"""


def ask_gemini(items):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log("[gemini] нет ключа — режим заголовков")
        return None
    model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
    news = [{k: it[k] for k in ("id", "source", "rubric", "title", "summary")} for it in items[:MAX_ITEMS_TO_MODEL]]
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            top=TOP_N, rubrics=", ".join(RUBRICS), news=json.dumps(news, ensure_ascii=False))}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=120, headers={"x-goog-api-key": key})
            if r.status_code == 429 or r.status_code >= 500:
                log(f"[gemini] {r.status_code}, повтор")
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:
            log(f"[gemini] ошибка: {e}")
            time.sleep(5)
    return None


def build(items, ai):
    by_id = {it["id"]: it for it in items}
    out = []
    if ai and ai.get("items"):
        for a in sorted(ai["items"], key=lambda x: -int(x.get("importance", 0) or 0)):
            srcs = [by_id[i] for i in a.get("ids", []) if i in by_id]
            if not srcs:
                continue
            first = srcs[0]
            out.append({
                "rubric": a.get("rubric") if a.get("rubric") in RUBRICS else first["rubric"],
                "title": a.get("title") or first["title"],
                "bullets": [b for b in (a.get("bullets") or []) if b][:3],
                "impact": a.get("impact") or "",
                "image": next((s["image"] for s in srcs if s["image"]), None),
                "sources": [{"name": s["source"], "url": s["url"]} for s in srcs],
                "published": first["published"],
            })
            if len(out) >= TOP_N:
                break
    if not out:  # запасной режим: свежие заголовки, по 3 из каждой рубрики
        per = {r: 0 for r in RUBRICS}
        for it in sorted(items, key=lambda x: x["published"] or "", reverse=True):
            if per.get(it["rubric"], 3) >= 3:
                continue
            per[it["rubric"]] += 1
            out.append({
                "rubric": it["rubric"], "title": it["title"],
                "bullets": [it["summary"][:220]] if it["summary"] else [],
                "impact": "", "image": it["image"],
                "sources": [{"name": it["source"], "url": it["url"]}],
                "published": it["published"],
            })
    now = datetime.now(TZ)
    return {
        "generated_at": now.isoformat(),
        "date_label": f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}",
        "edition": "Утренняя" if now.hour < 14 else "Вечерняя",
        "intro": (ai or {}).get("intro", ""),
        "key_figures": ((ai or {}).get("key_figures") or [])[:3],
        "ai": bool(ai and ai.get("items")),
        "items": out,
    }


def send(digest):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    app_url = os.environ.get("MINIAPP_URL", "").strip()
    if not token or not chats:
        log("[telegram] нет токена или чата — пропускаю отправку")
        return
    lines = [f"<b>{html.escape(digest['edition'])} сводка · {html.escape(digest['date_label'])}</b>"]
    if digest["intro"]:
        lines.append(f"\n{html.escape(digest['intro'])}")
    lines.append("")
    for it in digest["items"][:6]:
        lines.append(f"• <b>{html.escape(it['rubric'])}.</b> {html.escape(it['title'])}")
    text = "\n".join(lines)
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if app_url:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть сводку", "url": app_url}]]}
    for chat in chats:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={**payload, "chat_id": chat}, timeout=30)
        log(f"[telegram] {chat}: {r.status_code} {r.text[:200]}")
        r.raise_for_status()


def main():
    items = collect()
    log(f"[collect] всего {len(items)}")
    if not items:
        log("Ни один источник не ответил — сводку не обновляю")
        sys.exit(1)
    digest = build(items, ask_gemini(items))
    fill_images(digest)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(digest, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[out] {len(digest['items'])} новостей, ИИ: {digest['ai']}")
    if os.environ.get("DRY_RUN") != "1":
        send(digest)


if __name__ == "__main__":
    main()
