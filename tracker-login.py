import http.server,urllib.parse,urllib.request,json,secrets,hashlib,base64,pathlib,time
root=pathlib.Path(__file__).resolve().parent
(root/'data').mkdir(exist_ok=True)
state=secrets.token_urlsafe(24); verifier=secrets.token_urlsafe(48)
challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
endpoint='https://hello.rdclr.ru/realms/rc_realm/protocol/openid-connect'
redirect='http://localhost:9876/callback'
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
  if q.get('state')!=[state] or not q.get('code'):
   self.send_response(400); self.end_headers(); self.wfile.write(b'Login failed'); return
  body=urllib.parse.urlencode(dict(grant_type='authorization_code',client_id='web_tracker',redirect_uri=redirect,code=q['code'][0],code_verifier=verifier)).encode()
  try:
   data=json.load(urllib.request.urlopen(urllib.request.Request(endpoint+'/token',data=body),timeout=20))
   data['expires_at']=time.time()+data.get('expires_in',0)
   (root/'data/trck-tokens.json').write_text(json.dumps(data),encoding='utf8')
   self.send_response(200); self.end_headers(); self.wfile.write(b'Login successful. Return to Codex.'); self.server.done=True
  except Exception as e:
   self.send_response(500); self.end_headers(); self.wfile.write(b'Login failed'); print(type(e).__name__)
server=http.server.HTTPServer(('127.0.0.1',9876),Handler);server.timeout=1;server.done=False
url=endpoint+'/auth?'+urllib.parse.urlencode(dict(response_type='code',client_id='web_tracker',redirect_uri=redirect,scope='openid profile email',state=state,code_challenge=challenge,code_challenge_method='S256'))
import webbrowser
webbrowser.open(url)
print(url,flush=True)
end=time.time()+300
while not server.done and time.time()<end:server.handle_request()
server.server_close();print('Authorized' if server.done else 'Login timed out',flush=True)
