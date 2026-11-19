"""Authorize the Twitch chat-reading account and save tokens to .env locally."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json
import os
import secrets
import threading

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / '.env'
REDIRECT_URI = 'http://localhost:3000'
SCOPE = 'user:read:chat'


def read_env():
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env(values):
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding='utf-8').splitlines()
    keys = {'TWITCH_USER_ACCESS_TOKEN', 'TWITCH_REFRESH_TOKEN'}
    kept = [line for line in lines if not any(line.startswith(k + '=') for k in keys)]
    kept += [f'TWITCH_USER_ACCESS_TOKEN={values["access_token"]}',
             f'TWITCH_REFRESH_TOKEN={values["refresh_token"]}']
    ENV_FILE.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass


def main():
    env = read_env()
    client_id = env.get('TWITCH_CLIENT_ID') or os.getenv('TWITCH_CLIENT_ID', '')
    client_secret = env.get('TWITCH_CLIENT_SECRET') or os.getenv('TWITCH_CLIENT_SECRET', '')
    if not client_id or not client_secret:
        print('Сначала заполни TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET в файле .env')
        return

    state = secrets.token_urlsafe(24)
    result = {}
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            path_ok = parsed.path == '/'
            state_match = params.get('state', [''])[0] == state
            code_present = bool(params.get('code', [''])[0])
            print(f'OAuth callback: path_ok={path_ok}, state_match={state_match}, code_present={code_present}', flush=True)
            if not path_ok or not state_match:
                self.send_response(400)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write('<meta charset="utf-8"><h2>Ссылка устарела или не совпадает с текущим запросом. Закрой окно и используй последнюю ссылку из чата.</h2>'.encode('utf-8'))
                return
            if 'error' in params:
                result['error'] = params.get('error_description', params['error'])[0]
            else:
                result['code'] = params.get('code', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('<h2>Ответ Twitch получен. Можно закрыть это окно и вернуться в программу.</h2>'.encode())
            finished.set()

        def log_message(self, *_args):
            pass

    try:
        server = HTTPServer(('localhost', 3000), Handler)
    except OSError:
        print('Не удалось занять localhost:3000. Закрой программу, использующую этот порт, и повтори.')
        return
    server.timeout = 1
    query = urlencode({'client_id': client_id, 'redirect_uri': REDIRECT_URI,
                       'response_type': 'code', 'scope': SCOPE, 'state': state,
                       'force_verify': 'true'})
    auth_url = 'https://id.twitch.tv/oauth2/authorize?' + query
    print('Открой ссылку для авторизации Twitch в браузере:')
    print(auth_url, flush=True)
    try:
        while not finished.is_set():
            server.handle_request()
    finally:
        server.server_close()

    if result.get('error'):
        print('Авторизация отменена:', result['error'])
        return
    if not result.get('code'):
        print('Не удалось получить код авторизации. Повтори запуск.')
        return

    body = urlencode({'client_id': client_id, 'client_secret': client_secret,
                      'code': result['code'], 'grant_type': 'authorization_code',
                      'redirect_uri': REDIRECT_URI}).encode()
    req = Request('https://id.twitch.tv/oauth2/token', data=body,
                  headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urlopen(req, timeout=20) as response:
            tokens = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError) as exc:
        print('Twitch не выдал токен. Проверь Client ID, Secret и Redirect URL. Ошибка:', exc)
        return
    if not tokens.get('access_token') or not tokens.get('refresh_token'):
        print('Twitch вернул неполный ответ. Запусти авторизацию ещё раз.')
        return
    write_env(tokens)
    from app.tokens import save_tokens
    database = Path(env.get("DATABASE_PATH") or os.getenv("DATABASE_PATH", "./data/watcher.sqlite3"))
    token_path = Path(env.get("TWITCH_TOKEN_PATH") or os.getenv("TWITCH_TOKEN_PATH", "") or str(database.parent / "twitch-tokens.json"))
    if not token_path.is_absolute():
        token_path = ROOT / token_path
    save_tokens(token_path, tokens["access_token"], tokens["refresh_token"])
    print('Готово! Токены сохранены в .env на этом компьютере. Не отправляй файл .env никому.')
    print('Выданные разрешения:', ', '.join(tokens.get('scope', [])) or 'не указаны')


if __name__ == '__main__':
    main()
