"""
Tiger.M.M Web Chat - minimal web UI.
Start: python web_chat.py
Open: http://localhost:8800
"""
import asyncio, json, sys, os, threading, queue
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.chdir(str(Path(__file__).parent))

try:
    from flask import Flask, request, jsonify, Response, send_file
except ImportError:
    print("需要 Flask: pip install flask")
    sys.exit(1)

from config.settings import DATA_DIR
from core.model_client import ModelClient
from core.pipeline import Level4Pipeline

app = Flask(__name__)
pipeline = None
model_client = None

HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tiger.M.M · 星港</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0502;--gold:#FFAC02;--fg:#ccc;--dim:#555;--card:rgba(255,172,2,.04);--border:rgba(255,172,2,.12)}
body{background:var(--bg);color:var(--fg);font:15px/1.6 'Microsoft YaHei',sans-serif;height:100vh;display:flex;overflow:hidden}
#sidebar{width:250px;min-width:250px;background:#0d0803;border-right:1px solid var(--border);display:flex;flex-direction:column;padding:18px 14px}
#logo{font-size:1.15em;color:var(--gold);font-weight:bold;letter-spacing:2px}
#logo-sub{font-size:.68em;color:var(--dim);margin:2px 0 18px;letter-spacing:1px}
.side-title{font-size:.66em;color:var(--dim);letter-spacing:2px;margin:0 0 8px;text-transform:uppercase}
.side-section{margin-bottom:18px}
#model-switch{display:flex;gap:6px;flex-wrap:wrap}
#model-switch button{background:transparent;border:1px solid var(--border);color:var(--dim);padding:4px 12px;border-radius:4px;cursor:pointer;font-size:.76em;transition:.15s}
#model-switch button:hover{color:var(--gold);border-color:rgba(255,172,2,.35)}
#model-switch button.active{color:var(--gold);border-color:var(--gold);background:var(--card)}
.stat-row{display:flex;justify-content:space-between;font-size:.78em;color:var(--dim);padding:3px 0}
.stat-row b{color:var(--fg);font-weight:normal}
#inbox{font-size:.78em;overflow-y:auto}
.inbox-item{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:8px}
.inbox-title{color:var(--gold);font-size:.84em;margin-bottom:3px}
.inbox-body{color:#888;font-size:.74em;white-space:pre-wrap;word-break:break-all;max-height:90px;overflow:auto;margin-bottom:7px}
.inbox-actions{display:flex;gap:6px}
.inbox-actions button{flex:1;background:var(--card);border:1px solid var(--border);color:var(--gold);padding:4px 0;border-radius:4px;cursor:pointer;font-size:.74em}
.inbox-actions button:hover{background:rgba(255,172,2,.1)}
.inbox-actions button.deny{color:#c66;border-color:rgba(200,90,70,.3)}
.inbox-empty{color:var(--dim);font-size:.74em;text-align:center;padding:6px 0}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#topbar{padding:9px 18px;border-bottom:1px solid var(--border);font-size:.74em;color:var(--dim);letter-spacing:.5px;background:#0a0502}
#topbar b{color:var(--gold);font-weight:normal}
#chat{flex:1;overflow-y:auto;padding:18px 20px}
.msg{max-width:80%;margin:10px 0;padding:10px 14px;border-radius:10px;word-break:break-word;white-space:pre-wrap}
.msg .model{font-size:.66em;color:var(--dim);margin-bottom:4px;letter-spacing:.5px}
.user{margin-left:auto;background:var(--card);border:1px solid var(--border);border-bottom-right-radius:2px;color:var(--fg)}
.assistant{background:#0d0803;border:1px solid rgba(255,172,2,.07);border-bottom-left-radius:2px;color:var(--fg)}
details.trace{margin-top:8px;border-top:1px dashed var(--border);padding-top:6px;font-size:.72em}
details.trace summary{cursor:pointer;color:var(--dim);user-select:none}
details.trace summary:hover{color:var(--gold)}
.trace-item{padding:4px 0;border-bottom:1px dashed rgba(255,172,2,.08)}
.trace-item:last-child{border-bottom:none}
.trace-head b{color:var(--gold)}
.trace-head .ok{color:#6a6}
.trace-head .fail{color:#c66}
.trace-args{color:#777;font-size:.9em;white-space:pre-wrap;word-break:break-all;max-height:50px;overflow:auto}
.trace-result{color:#999;font-size:.9em;white-space:pre-wrap;word-break:break-all;max-height:100px;overflow:auto}
#input-area{display:flex;gap:10px;padding:14px 18px;border-top:1px solid var(--border);background:#0a0502}
#input{flex:1;background:#0d0803;color:var(--fg);border:1px solid var(--border);padding:11px 14px;border-radius:8px;font-size:14px;outline:none;resize:none;font-family:inherit;line-height:1.5;max-height:160px}
#input:focus{border-color:var(--gold)}
#send{background:var(--gold);color:#0a0502;border:none;padding:0 26px;border-radius:8px;cursor:pointer;font-weight:bold;font-size:14px;letter-spacing:1px}
#send:hover{opacity:.85}
.msg img{max-width:100%;border-radius:6px;border:1px solid var(--border);margin:4px 0}
</style>
</head>
<body>
<div id="sidebar">
  <div id="logo">TIGER.M.M</div>
  <div id="logo-sub">虎哥·凌霄 · 星港驾驶舱</div>
  <div class="side-section">
    <div class="side-title">模型</div>
    <div id="model-switch">
      <button data-model="auto" class="active">auto</button>
      <button data-model="deepseek">deepseek</button>
      <button data-model="mimo">mimo</button>
      <button data-model="ollama">ollama</button>
    </div>
  </div>
  <div class="side-section">
    <div class="side-title">状态</div>
    <div id="stats"><div class="stat-row"><span>加载中</span></div></div>
  </div>
  <div class="side-section" style="flex:1;overflow-y:auto;min-height:0">
    <div class="side-title">待审批</div>
    <div id="inbox"><div class="inbox-empty">无待审批</div></div>
  </div>
</div>
<div id="main">
  <div id="topbar">连接中...</div>
  <div id="chat"></div>
  <div id="input-area">
    <textarea id="input" rows="1" placeholder="直接说，虎哥干活...  (Enter 发送 / Shift+Enter 换行)" autofocus></textarea>
    <button id="send">发送</button>
  </div>
</div>
<script>
const chat=document.getElementById('chat'),input=document.getElementById('input'),
      send=document.getElementById('send'),topbar=document.getElementById('topbar');
let model='auto';
function updateModelSwitch(){
  document.querySelectorAll('#model-switch button').forEach(b=>{
    b.classList.toggle('active',b.dataset.model===model)
  });
}
document.querySelectorAll('#model-switch button').forEach(b=>{
  b.onclick=()=>{model=b.dataset.model;updateModelSwitch()}
});
function autoGrow(){input.style.height='auto';input.style.height=Math.min(input.scrollHeight,160)+'px'}
input.addEventListener('input',autoGrow);
async function doSend(){
  const msg=input.value.trim();if(!msg)return;
  if(msg.startsWith('@')){model=msg.slice(1).trim();updateModelSwitch();input.value='';autoGrow();return}
  addMsg('user',msg);input.value='';autoGrow();
  if(msg==='/clear'){chat.innerHTML='';return}
  addMsg('assistant','...',model);
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,model:model})});
    if(!r.ok){updateLast('Error: '+r.status);return}
    const reader=r.body.getReader(),decoder=new TextDecoder();
    let full='';
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      const text=decoder.decode(value,{stream:true});
      const lines=text.split('\n');
      for(const l of lines){
        if(l.startsWith('data:')){
          const d=JSON.parse(l.slice(5));
          if(d.text){full+=d.text;updateLast(full)}
          if(d.done){topbar.innerHTML='模型 <b>'+d.model+'</b> · '+d.elapsed+'s';if(d.trace&&d.trace.length)renderTrace(d.trace);return}
          if(d.error){updateLast('Error: '+d.error);return}
        }
      }
    }
  }catch(e){updateLast('Error: '+e)}
}
function renderRich(text){
  let s=esc(text||'');
  s=s.replace(/([A-Za-z]:[\\/][^\s<>"']+?\.(png|jpg|jpeg|gif|bmp|webp))/gi, function(m){
    return '<img src="/image?path='+encodeURIComponent(m)+'">';
  });
  return s;
}
function addMsg(role,text,m){
  const d=document.createElement('div');
  d.className='msg '+role;
  if(m)d.innerHTML='<div class=model>'+esc(m)+'</div>'+renderRich(text);
  else d.textContent=text;
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
}
function updateLast(text){
  const ms=chat.querySelectorAll('.assistant');if(!ms.length)return;
  ms[ms.length-1].innerHTML=renderRich(text);
  chat.scrollTop=chat.scrollHeight;
}
function esc(s){return String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function renderTrace(trace){
  const ms=chat.querySelectorAll('.assistant');if(!ms.length)return;
  const box=document.createElement('details');box.className='trace';
  box.innerHTML='<summary>工具轨迹 ('+trace.length+')</summary>';
  trace.forEach(t=>{
    const item=document.createElement('div');item.className='trace-item';
    const ok=!!t.success;
    item.innerHTML=
      '<div class="trace-head"><b>#'+t.round+'</b> '+esc(t.tool)+' <span class="'+(ok?'ok':'fail')+'">'+(ok?'✓':'✗')+'</span></div>'+
      '<div class="trace-args">'+esc(t.args)+'</div>'+
      '<div class="trace-result">'+esc(t.result)+'</div>';
    box.appendChild(item);
  });
  ms[ms.length-1].appendChild(box);chat.scrollTop=chat.scrollHeight;
}
send.onclick=doSend;
input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();doSend()}};
(async function loadHistory(){
  try{
    const r=await fetch('/history');const d=await r.json();
    (d.history||[]).forEach(t=>{if(t.role&&t.text)addMsg(t.role,t.text)});
    topbar.textContent='已恢复 '+((d.history||[]).length)+' 条历史';
  }catch(e){}
})();
async function pollInbox(){
  try{
    const r=await fetch('/inbox');const d=await r.json();
    const box=document.getElementById('inbox');
    if(!d.items||!d.items.length){box.innerHTML='<div class="inbox-empty">无待审批</div>';return}
    box.innerHTML='';
    d.items.forEach(it=>{
      const div=document.createElement('div');div.className='inbox-item';
      const cmds=(it.cmds||[]).join('\n')||it.body||'';
      div.innerHTML=
        '<div class="inbox-title">'+esc(it.title||'')+'</div>'+
        '<div class="inbox-body">'+esc(cmds)+'</div>'+
        '<div class="inbox-actions">'+
          '<button onclick="resolveInbox(\''+it.id+'\',\'allow\')">允许</button>'+
          '<button class="deny" onclick="resolveInbox(\''+it.id+'\',\'deny\')">拒绝</button>'+
        '</div>';
      box.appendChild(div);
    });
  }catch(e){}
}
async function resolveInbox(id,res){
  try{
    await fetch('/inbox/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:id,resolution:res})});
    pollInbox();
  }catch(e){}
}
function fmtUptime(s){
  const h=Math.floor(s/3600),m=Math.floor(s%3600/60);
  return h>0?(h+'h'+m+'m'):(m+'m'+(s%60)+'s');
}
async function pollStats(){
  try{
    const r=await fetch('/stats');const d=await r.json();
    const st=document.getElementById('stats');
    if(d.error){st.textContent=d.error;return}
    st.innerHTML=
      '<div class="stat-row"><span>运行</span><b>'+fmtUptime(d.uptime)+'</b></div>'+
      '<div class="stat-row"><span>请求</span><b>'+d.total+'</b></div>'+
      '<div class="stat-row"><span>错误</span><b>'+d.errors+'</b></div>'+
      '<div class="stat-row"><span>会话</span><b>'+d.sessions+'</b></div>';
  }catch(e){}
}
setInterval(pollStats,5000);pollStats();
setInterval(pollInbox,5000);pollInbox();
</script>
</body>
</html>"""

@app.route('/')
def index():
    # ★ 操作界面: 优先服务 ui/app.html (WorkBuddy 式三模式界面)
    try:
        p = Path(__file__).parent / "ui" / "app.html"
        if p.exists():
            return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return HTML


def _mode_now():
    try:
        return pipeline.modes.get()
    except Exception:
        return 'craft'


def _mode_payload(result):
    """把三模式相关字段拼进 SSE done 事件。"""
    try:
        return {
            'mode': result.get('mode') or _mode_now(),
            'intent': result.get('intent', ''),
            'pending_confirm': bool(result.get('pending_confirm')),
            'plan': result.get('plan') or [],
            'plan_message': result.get('plan_message', ''),
        }
    except Exception:
        return {'mode': _mode_now()}


@app.route('/modes')
def modes_list():
    """三模式清单 + 当前模式。"""
    try:
        cur = pipeline.modes.info()
        return jsonify({"current": cur['mode'], "info": cur,
                        "modes": pipeline.modes.all(),
                        "order": cur.get('order', ['ask', 'plan', 'craft']),
                        "pending": bool(pipeline.modes.get_pending())})
    except Exception as e:
        return jsonify({"current": "craft", "modes": {}, "error": str(e)})


@app.route('/mode', methods=['POST'])
def mode_set():
    """切换运行模式: {mode: ask|plan|craft}"""
    try:
        data = request.get_json() or {}
        m = (data.get('mode') or '').strip().lower()
        if not pipeline.modes.set(m):
            return jsonify({"ok": False, "error": pipeline.modes.set_error()})
        info = pipeline.modes.info()
        return jsonify({"ok": True, "current": info['mode'], "info": info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route('/chat', methods=['POST'])
def chat():
    data = request.get_json()
    msg = data.get('message', '')
    ext_model = data.get('model', 'auto')
    if ext_model == 'auto':
        ext_model = None
    # ★ 三模式: 前端选中的模式直接生效 (ask / plan / craft)
    _m = (data.get('mode') or '').strip().lower()
    if _m in ('ask', 'plan', 'craft'):
        try:
            pipeline.modes.set(_m)
        except Exception:
            pass

    def generate():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(pipeline.process(msg, ext_model=ext_model))
            # 记录 assistant 回复到会话历史（process 只记 user）
            try:
                pipeline.memory.add_turn("assistant", result.get('response', ''))
            except Exception:
                pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return
        
        model = result.get('model', 'local')
        elapsed = result.get('elapsed', 0)
        trace = result.get('trace', [])
        
        # Try streaming
        if result.get('_stream') and hasattr(model_client, 'generate_stream'):
            async def stream_tokens():
                full = ''
                async for chunk, err in model_client.generate_stream(
                    result['_model_name'], result['_messages']):
                    if err:
                        yield f"data: {json.dumps({'error': err})}\n\n"
                        return
                    if chunk is None and err is None:
                        _done = {'done': True, 'model': model, 'elapsed': elapsed, 'trace': trace}
                        _done.update(_mode_payload(result))
                        yield f"data: {json.dumps(_done)}\n\n"
                        return
                    if chunk:
                        full += chunk
                        yield f"data: {json.dumps({'text': chunk})}\n\n"
            try:
                for chunk_data in loop.run_until_complete(_stream_all(stream_tokens())):
                    yield chunk_data
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        else:
            text = result.get('response', '')
            _done = {'text': text, 'done': True, 'model': model,
                     'elapsed': elapsed, 'trace': trace}
            _done.update(_mode_payload(result))
            yield f"data: {json.dumps(_done)}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/history')
def history():
    try:
        turns = pipeline.memory.recent(50) if pipeline else []
        return jsonify({"history": turns})
    except Exception:
        return jsonify({"history": []})


@app.route('/inbox')
def inbox_list():
    try:
        from core.inbox import get_inbox
        items = get_inbox().list_pending()
        return jsonify({"count": len(items), "items": [i.to_dict() for i in items]})
    except Exception as e:
        return jsonify({"count": 0, "items": [], "error": str(e)})


@app.route('/inbox/resolve', methods=['POST'])
def inbox_resolve():
    try:
        from core.inbox import get_inbox, RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS
        data = request.get_json() or {}
        item_id = data.get('item_id', '')
        resolution = data.get('resolution', 'allow')
        if resolution not in (RESOLVE_ALLOW, RESOLVE_DENY, RESOLVE_ALWAYS):
            return jsonify({"resolved": False, "error": "invalid resolution"})
        ok = get_inbox().resolve(item_id, resolution)
        return jsonify({"resolved": ok})
    except Exception as e:
        return jsonify({"resolved": False, "error": str(e)})


@app.route('/image')
def image():
    try:
        from storage.file_secure import safe_path
        from config.settings import PROJECT_ROOT as _prj
        p = request.args.get('path', '')
        if not p.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
            return "not an image", 400
        target = safe_path(p, _prj)
        if not target.exists():
            return "not found", 404
        return send_file(str(target))
    except Exception as e:
        return f"error: {e}", 404


@app.route('/stats')
def stats():
    try:
        import time as _t
        uptime = int(_t.time() - pipeline._startup) if pipeline else 0
        total = pipeline.stats.get('total', 0) if pipeline else 0
        errors = pipeline.stats.get('errors', 0) if pipeline else 0
        sessions = pipeline.sm.stats().get('total', 0) if pipeline else 0
        from core.inbox import get_inbox
        pending = get_inbox().stats().get('pending', 0)
        return jsonify({"uptime": uptime, "total": total, "errors": errors,
                        "pending": pending, "sessions": sessions})
    except Exception as e:
        return jsonify({"error": str(e)})


async def _stream_all(agen):
    results = []
    async for item in agen:
        results.append(item)
    return results

def main():
    global pipeline, model_client
    config = {}
    keys_file = DATA_DIR / "keys.json"
    leg_keys = Path(os.getcwd()) / "keys.json"
    for kf in [keys_file, leg_keys]:
        if kf.exists():
            try:
                import json as _j
                data = _j.loads(kf.read_text(encoding='utf-8'))
                for k, v in data.items():
                    if isinstance(v, dict) and ('key' in v or 'api_key' in v):
                        config[k] = v
            except: pass
    
    model_client = ModelClient(config)
    pipeline = Level4Pipeline(model_client, config)
    
    from waitress import serve
    print("Tiger.M.M Web Chat -> http://localhost:8800 (waitress)")
    serve(app, host='0.0.0.0', port=8800, threads=8)

if __name__ == '__main__':
    main()
