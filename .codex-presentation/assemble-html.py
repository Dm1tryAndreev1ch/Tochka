from pathlib import Path
import base64
s=Path('.codex-presentation/tochka-slides.template.html').read_text().replace('выключчена','выключена')
s=s.replace("if(overview.open||e.target.closest('select,button,input,textarea,[contenteditable]'))return;", "if(overview.open||e.target.closest('select,input,textarea,[contenteditable]')||(e.target.closest('button')&&e.key===' '))return;")
s=s.replace('</style>','\n'+Path('.codex-presentation/cinematic.css').read_text()+'\n</style>').replace('</body>','<script>\n'+Path('.codex-presentation/cinematic.js').read_text()+'\n</script>\n</body>')
def enc(path,mime):return 'data:'+mime+';base64,'+base64.b64encode(Path(path).read_bytes()).decode()
s=s.replace('FONT_DATA',enc('app/static/golos.ttf','font/ttf')).replace('LOGO_LIGHT',enc('app/static/brand/tochka-light.svg','image/svg+xml')).replace('SIGNAL_DARK',enc('app/static/brand/signal.svg','image/svg+xml'))
signal=Path('app/static/brand/signal.svg').read_text().replace('#17121d','#f2f0f4');s=s.replace('SIGNAL_LIGHT','data:image/svg+xml;base64,'+base64.b64encode(signal.encode()).decode())
Path('output/tochka-html/tochka-slides.html').write_text(s)
print('HTML assembled:',len(s),'characters')
