"""get_chat_id.py — 텔레그램 chat_id 자동 추출 도우미.
1) .env 에 TELEGRAM_BOT_TOKEN 입력  2) 텔레그램에서 내 봇에게 아무 메시지 전송
3) python get_chat_id.py  → 나오는 chat_id 를 .env 의 TELEGRAM_CHAT_ID 에 입력
"""
import os
import requests
from dotenv import load_dotenv
load_dotenv()

token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    print("먼저 .env 에 TELEGRAM_BOT_TOKEN 을 넣으세요."); raise SystemExit

me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10).json()
if not me.get("ok"):
    print("봇 토큰이 올바르지 않습니다:", me); raise SystemExit
print(f"봇 확인: @{me['result']['username']}")

r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10).json()
ids = {}
for u in r.get("result", []):
    chat = (u.get("message") or u.get("edited_message") or {}).get("chat")
    if chat:
        ids[chat["id"]] = chat.get("first_name") or chat.get("title") or "?"

if not ids:
    print("\n아직 메시지가 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
else:
    print("\n발견된 chat_id:")
    for cid, name in ids.items():
        print(f"   TELEGRAM_CHAT_ID={cid}   ({name})")
