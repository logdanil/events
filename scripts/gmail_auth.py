"""Одноразовое получение GMAIL_REFRESH_TOKEN для отправки почты через Gmail API.

Запуск (локально, не на сервере):
    .venv/bin/python scripts/gmail_auth.py CLIENT_ID CLIENT_SECRET

Скрипт откроет браузер: войдите в АККАУНТ-ОТПРАВИТЕЛЬ (moexderivatives@gmail.com)
и разрешите доступ «Отправка писем от вашего имени». Полученный refresh-токен
выведется в терминал — добавьте его в Railway как GMAIL_REFRESH_TOKEN.
"""
from __future__ import annotations

import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"
SCOPE = "https://www.googleapis.com/auth/gmail.send"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Использование: python scripts/gmail_auth.py CLIENT_ID CLIENT_SECRET")
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)
    received: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received["code"] = params.get("code", [""])[0]
            received["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Готово — вернитесь в терминал.</h2>".encode())

        def log_message(self, *args):  # тишина в терминале
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # выдаёт refresh-токен
        "prompt": "consent",        # гарантирует выдачу refresh-токена повторно
        "state": state,
    })
    print("Открываю браузер для авторизации…\nЕсли не открылся, перейдите вручную:\n", auth_url)
    webbrowser.open(auth_url)
    thread.join(timeout=300)
    if not received.get("code"):
        raise SystemExit("Не дождался ответа от Google (5 минут). Попробуйте ещё раз.")
    if received.get("state") != state:
        raise SystemExit("state не совпал — попробуйте ещё раз.")

    response = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": received["code"],
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }, timeout=30)
    response.raise_for_status()
    tokens = response.json()
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise SystemExit(f"Google не вернул refresh_token: {tokens}. "
                         "Убедитесь, что в запросе был prompt=consent, и повторите.")
    print("\n✅ Успех! Добавьте в Railway → Variables:\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={refresh}")
    print("EMAIL_FROM=адрес_аккаунта_в_котором_вы_только_что_авторизовались")


if __name__ == "__main__":
    main()
