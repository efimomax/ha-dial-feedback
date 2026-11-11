#!/usr/bin/env python3
"""Checks the announcement threads for new replies and sends them to Telegram.

Sources (monitor/topics.json):
  * Discourse forums (public JSON, no login needed);
  * Reddit posts via the official OAuth API (app-only token), only if REDDIT_CLIENT_ID and
    REDDIT_CLIENT_SECRET are set.
Already reported reply ids are kept in monitor/state.json, which the workflow commits back.
The first run for a thread only records the existing replies, without notifying.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional: without them the messages are printed),
     REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET (optional), DRY_RUN=1 (print only, keep state).
"""
import base64
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOPICS = os.path.join(HERE, "topics.json")
STATE = os.path.join(HERE, "state.json")
USER_AGENT = "script:ha-dial-feedback-monitor:1.0 (by /u/efimomax)"
EXCERPT = 500


def http_json(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def plain(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def short(text):
    return text if len(text) <= EXCERPT else text[:EXCERPT] + "…"


def discourse_replies(topic):
    data = http_json(f"{topic['base']}/t/{topic['topic_id']}.json")
    slug = data.get("slug", "")
    for post in data["post_stream"]["posts"]:
        yield {
            "id": f"d:{topic['base']}:{post['id']}",
            "author": post["username"],
            "text": plain(post.get("cooked")),
            "url": f"{topic['base']}/t/{slug}/{topic['topic_id']}/{post['post_number']}",
        }


def reddit_token():
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    data = http_json("https://www.reddit.com/api/v1/access_token",
                     headers={"Authorization": f"Basic {auth}"}, data=body)
    return data["access_token"]


def reddit_replies(post, token):
    data = http_json(f"https://oauth.reddit.com/comments/{post['post_id']}?limit=500&depth=10&raw_json=1",
                     headers={"Authorization": f"Bearer {token}"})

    def walk(children):
        for child in children:
            if child.get("kind") != "t1":
                continue
            c = child["data"]
            yield {
                "id": f"r:{c['id']}",
                "author": c.get("author", "?"),
                "text": plain(c.get("body")),
                "url": f"https://www.reddit.com{c.get('permalink', '')}",
            }
            replies = c.get("replies")
            if isinstance(replies, dict):
                yield from walk(replies["data"]["children"])

    yield from walk(data[1]["data"]["children"])


def notify(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if os.environ.get("DRY_RUN") or not token or not chat:
        print("---\n" + text)
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    http_json(f"https://api.telegram.org/bot{token}/sendMessage", data=body)


def main():
    config = json.load(open(TOPICS, encoding="utf-8"))
    owners = {name.lower() for name in config.get("owner_names", [])}
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    sources = [(t["name"], lambda t=t: discourse_replies(t)) for t in config.get("discourse", [])]

    token = None
    if config.get("reddit"):
        try:
            token = reddit_token()
        except Exception as e:  # noqa: BLE001 - report and keep checking the forums
            print(f"reddit auth failed: {e}", file=sys.stderr)
        if token is None:
            print("reddit: no API credentials, skipped", file=sys.stderr)
        else:
            sources += [(p["name"], lambda p=p: reddit_replies(p, token)) for p in config["reddit"]]

    errors = 0
    for name, fetch in sources:
        try:
            replies = list(fetch())
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"{name}: {e}", file=sys.stderr)
            continue
        first_run = name not in state
        seen = set(state.get(name, []))
        new = [r for r in replies if r["id"] not in seen]
        for r in new:
            if not first_run and r["author"].lower() not in owners:
                notify(f"HA Dial — новый ответ\n{name}\nот {r['author']}\n\n{short(r['text'])}\n\n{r['url']}")
        state[name] = sorted(seen | {r["id"] for r in replies})
        print(f"{name}: {len(replies)} replies, {len(new)} new{' (first run, silent)' if first_run else ''}")

    if not os.environ.get("DRY_RUN"):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
    return 1 if errors and errors == len(sources) else 0


if __name__ == "__main__":
    sys.exit(main())
