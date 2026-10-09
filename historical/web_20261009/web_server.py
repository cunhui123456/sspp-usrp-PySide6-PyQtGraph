"""仅本机浏览器访问的 HTTP 服务；启动服务不会初始化 USRP。"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import parse_qs,urlsplit
from .controller import DEFAULTS,LabController,LabError

WEB=Path(__file__).resolve().parent/'web'

def create_server(port=8768,controller=None):
    lab=controller or LabController()
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            # 不将 token、标签或查询参数写入终端请求日志。
            pass
        def local_request(self):
            host=self.headers.get('Host','')
            allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            if host not in allowed: raise LabError('只允许本机访问',403)
            origin=self.headers.get('Origin')
            if origin and origin not in {f'http://{x}' for x in allowed}: raise LabError('请求来源无效',403)
        def respond(self,data,status=200,content_type='application/json; charset=utf-8'):
            body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if content_type.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            try:
                self.local_request()
                url=urlsplit(self.path);query=parse_qs(url.query)
                if url.path=='/':
                    body=(WEB/'index.html').read_text().replace('__TOKEN__',token).encode()
                    return self.respond(body,content_type='text/html; charset=utf-8')
                if url.path in ('/app.js','/plots.js','/style.css'):
                    p=WEB/url.path[1:]
                    return self.respond(p.read_bytes(),content_type=('text/javascript' if p.suffix=='.js' else 'text/css')+'; charset=utf-8')
                if url.path=='/api/bootstrap': return self.respond({'defaults':DEFAULTS,'profiles':lab.refresh_profiles(),'real_hardware_only':True})
                if url.path=='/api/status': return self.respond(lab.snapshot())
                if url.path=='/api/curve': return self.respond(lab.curve())
                if url.path=='/api/diagnostics': return self.respond(lab.diagnostic_view(query.get('key',[None])[0]))
                if url.path=='/api/history': return self.respond(lab.history())
                if url.path=='/api/run': return self.respond(lab.details(query.get('key',[''])[0]))
                if url.path=='/api/file':
                    path=lab.download(query.get('key',[''])[0],query.get('name',[''])[0])
                    mime=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    self.send_response(200);self.send_header('Content-Type',mime)
                    self.send_header('Content-Length',str(path.stat().st_size));self.send_header('X-Content-Type-Options','nosniff')
                    # 浏览器预览 PNG/PDF，其余文件作为附件；名字仅来自受控目录白名单。
                    if path.suffix not in ('.png','.pdf'): self.send_header('Content-Disposition','attachment')
                    self.end_headers()
                    with path.open('rb') as source:
                        while data:=source.read(1024*1024): self.wfile.write(data)
                    return
                raise LabError('页面不存在',404)
            except LabError as exc: self.respond({'error':str(exc)},exc.status)
            except (BrokenPipeError,ConnectionResetError): pass
            except Exception: self.respond({'error':'读取失败，请查看服务终端'},500)
        def do_POST(self):
            try:
                self.local_request()
                if self.headers.get('X-SSPP-Token')!=token: raise LabError('页面会话已失效，请刷新页面',403)
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536: raise LabError('请求长度无效')
                if not self.headers.get('Content-Type','').startswith('application/json'): raise LabError('请求格式无效')
                def reject_constant(value): raise ValueError('nonfinite JSON')
                value=json.loads(self.rfile.read(length),parse_constant=reject_constant)
                if not isinstance(value,dict): raise LabError('请求必须是对象')
                path=urlsplit(self.path).path
                if path=='/api/plan': return self.respond(lab.preflight(value))
                if path=='/api/start': return self.respond(lab.start(value),202)
                if path=='/api/stop': return self.respond(lab.stop())
                if path=='/api/export': return self.respond(lab.export_diagnostics(value.get('key')))
                if path=='/api/mark': return self.respond(lab.mark(value.get('label'),value.get('settle',3),value.get('contact')))
                raise LabError('接口不存在',404)
            except LabError as exc: self.respond({'error':str(exc)},exc.status)
            except (ValueError,TypeError): self.respond({'error':'请求内容无效'},400)
            except (BrokenPipeError,ConnectionResetError): pass
            except Exception as exc: self.respond({'error':lab.public_log(str(exc))},500)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    server.lab=lab
    return server

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8768)
    args=parser.parse_args()
    if not 1024<=args.port<=65535: parser.error('port must be 1024–65535')
    server=create_server(args.port)
    stopping=threading.Event()
    def stop(signum,frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    print(f'SSPP 实验软件：http://127.0.0.1:{server.server_port}',flush=True)
    print('服务已启动；尚未打开设备或启动收发。关闭终端服务会请求停止当前实验。',flush=True)
    try: server.serve_forever(poll_interval=.2)
    finally:
        server.lab.shutdown();server.server_close()
    return 0
