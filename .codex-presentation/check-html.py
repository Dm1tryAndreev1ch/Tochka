from pathlib import Path
import subprocess,json,time,sys
root=Path.cwd();ab=root/'node_modules/.bin/agent-browser';folder=root/(sys.argv[1] if len(sys.argv)>1 else '.codex-presentation/html-check');folder.mkdir(exist_ok=True)
def run(*args):
 p=subprocess.run([str(ab),'--session','tochka-deck',*args],capture_output=True,text=True)
 if p.returncode:raise RuntimeError(p.stdout+p.stderr)
 return p.stdout
run('eval',"(()=>{document.body.classList.add('qa');const st=document.createElement('style');st.id='qa-style';st.textContent='.qa *, .qa *::before,.qa *::after{animation:none!important;transition:none!important}.qa .slide{clip-path:none!important}.qa .word{transform:none!important;opacity:1!important}.qa .drawn{stroke-dashoffset:0!important}';document.head.append(st)})()")
reports=[]
for i in range(12):
 run('select','#slide-select',str(i));time.sleep(.05)
 js="""JSON.stringify((()=>{const s=document.querySelector('.slide.is-active'),r=s.getBoundingClientRect(),bad=[...s.querySelectorAll('h1,h2,h3,p,.price,.value,.orbit-label,.bar-label')].map(e=>({e,r:e.getBoundingClientRect()})).filter(x=>x.r.width&&x.r.height&&(x.r.left<r.left-2||x.r.right>r.right+2||x.r.top<r.top-2||x.r.bottom>r.bottom+2)).map(x=>({text:x.e.textContent,bounds:x.r.toJSON()}));return{slide:s.dataset.title,overflow:bad,scrollX:document.documentElement.scrollWidth>innerWidth,brokenImages:[...document.images].filter(x=>x.closest('.is-active')&&(!x.complete||!x.naturalWidth)).length}})())"""
 out=run('eval',js)
 try:
  data=json.loads(out)
  if isinstance(data,str):data=json.loads(data)
 except Exception:data={'raw':out}
 reports.append(data)
 run('screenshot','#screen',str(folder/f'{i+1:02d}.png'))
print(json.dumps(reports,ensure_ascii=False,indent=2))
(folder/'desktop-report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2))
