"""Multi-Agent Orchestrator - parallel task execution."""
import sys,os,subprocess,concurrent.futures
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))

AGENTS = {
    'code': {'desc':'Code','model':'deepseek','tmpl':'Write code: TASK'},
    'write': {'desc':'Writing','model':'mimo','tmpl':'Write creatively: TASK'},
    'general': {'desc':'General','model':'deepseek','tmpl':'Answer: TASK'},
}

def classify(t):
    t=t.lower()
    if any(k in t for k in ['代码','def','算法']):return'code'
    if any(k in t for k in ['文案','诗歌','故事','润色']):return'write'
    return'general'

def run_one(task,agent_type=None):
    if not agent_type:agent_type=classify(task)
    a=AGENTS.get(agent_type,AGENTS['general'])
    prompt=a['tmpl'].replace('TASK',task)
    model=a['model']
    p=subprocess.Popen([sys.executable,'main.py','--cli','--no-check'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,cwd=str(Path(__file__).parent))
    out,_=p.communicate(input='@'+model+'\n'+prompt+'\n/exit\n',timeout=120)
    text=''
    for l in out.split(chr(10)):
        if l.strip() and not any(x in l for x in ['Tiger.M.M','MARY','TOOLS']):
            text+=l.strip()+' '
    return {'agent':agent_type,'task':task,'output':text[:2000].strip(),'model':model}

def run_parallel(tasks):
    r=[]
    with concurrent.futures.ThreadPoolExecutor(4)as e:
        f={e.submit(run_one,t):t for t in tasks}
        for fu in concurrent.futures.as_completed(f):
            try:r.append(fu.result(timeout=120))
            except Exception as ex:r.append({'task':f[fu],'error':str(ex)})
    return r

if __name__=='__main__':
    if len(sys.argv)<2:
        print('Usage: python multi_agent.py task1|task2|task3');sys.exit(0)
    tasks=[t.strip() for t in sys.argv[1].split('|')]
    print(f'{len(tasks)} tasks parallel...')
    for r in run_parallel(tasks):
        a=r.get('agent','?');t=r.get('task','');o=r.get('output','');e=r.get('error','')
        print(f'\n[{a}] {t[:60]}')
        if e:print(f'  ERR: {e[:200]}')
        if o:print(f'  {o[:300]}')