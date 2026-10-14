import zipfile,xml.etree.ElementTree as E,struct,re
from pathlib import Path
P='http://schemas.openxmlformats.org/presentationml/2006/main'; A='http://schemas.openxmlformats.org/drawingml/2006/main'; R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
for prefix,ns in [('p',P),('a',A),('r',R)]:E.register_namespace(prefix,ns)
q=lambda n:'{'+P+'}'+n
ns={'p':P,'a':A}
with zipfile.ZipFile('.codex-presentation/build/draft.pptx') as z: data={n:z.read(n) for n in z.namelist()}
count=0
for name in list(data):
 if not re.fullmatch(r'ppt/slides/slide\d+.xml',name):continue
 root=E.fromstring(data[name]); groups={}
 for sp in root.findall('.//p:sp',ns):
  nv=sp.find('p:nvSpPr/p:cNvPr',ns)
  if nv is not None and nv.get('name','').startswith('reveal-'):
   g=int(nv.get('name').split('-')[1]);groups.setdefault(g,[]).append(nv.get('id'))
 trans=E.SubElement(root,q('transition'),{'spd':'med','advClick':'1'});E.SubElement(trans,q('fade'))
 timing=E.SubElement(root,q('timing')); tn=E.SubElement(timing,q('tnLst')); par=E.SubElement(tn,q('par')); rt=E.SubElement(par,q('cTn'),{'id':'1','dur':'indefinite','restart':'never','nodeType':'tmRoot'}); cl=E.SubElement(rt,q('childTnLst'))
 seq=E.SubElement(cl,q('seq'),{'concurrent':'1','nextAc':'seek'});main=E.SubElement(seq,q('cTn'),{'id':'2','dur':'indefinite','nodeType':'mainSeq'});children=E.SubElement(main,q('childTnLst'))
 idx=3
 for g,ids in sorted(groups.items()):
  for spid in ids:
   par=E.SubElement(children,q('par')); ct=E.SubElement(par,q('cTn'),{'id':str(idx),'fill':'hold'});idx+=1
   conds=E.SubElement(ct,q('stCondLst'));E.SubElement(conds,q('cond'),{'delay':str(100+(g-1)*260)})
   inner=E.SubElement(E.SubElement(ct,q('childTnLst')),q('par'))
   c=E.SubElement(inner,q('cTn'),{'id':str(idx),'presetID':'10','presetClass':'entr','presetSubtype':'0','fill':'hold','nodeType':'withEffect'});idx+=1
   E.SubElement(E.SubElement(c,q('stCondLst')),q('cond'),{'delay':'0'})
   effects=E.SubElement(c,q('childTnLst'))
   # Set visibility explicitly for reliable entrance playback.
   st=E.SubElement(effects,q('set'));bh=E.SubElement(st,q('cBhvr'));x=E.SubElement(bh,q('cTn'),{'id':str(idx),'dur':'1','fill':'hold'});idx+=1;E.SubElement(E.SubElement(x,q('stCondLst')),q('cond'),{'delay':'0'})
   E.SubElement(E.SubElement(bh,q('tgtEl')),q('spTgt'),{'spid':spid});E.SubElement(E.SubElement(bh,q('attrNameLst')),q('attrName')).text='style.visibility';E.SubElement(E.SubElement(st,q('to')),q('strVal'),{'val':'visible'})
   eff=E.SubElement(effects,q('animEffect'),{'transition':'in','filter':'fade'});bh=E.SubElement(eff,q('cBhvr'));E.SubElement(bh,q('cTn'),{'id':str(idx),'dur':'450'});idx+=1;E.SubElement(E.SubElement(bh,q('tgtEl')),q('spTgt'),{'spid':spid});count+=1
 for tag,evt in [('prevCondLst','onPrev'),('nextCondLst','onNext')]:
  co=E.SubElement(E.SubElement(seq,q(tag)),q('cond'),{'evt':evt,'delay':'0'});E.SubElement(E.SubElement(co,q('tgtEl')),q('sldTgt'))
 for ln in root.findall('.//a:tcPr/*',ns):
  if ln.tag.split('}')[-1].startswith('ln'):
   for ch in list(ln):ln.remove(ch)
   sf=E.SubElement(ln,'{'+A+'}solidFill');E.SubElement(sf,'{'+A+'}srgbClr',{'val':'D9D5DE'});ln.set('w','6350')
   if ln.tag.split('}')[-1] in ('lnL','lnR'):
    ln.remove(sf);E.SubElement(ln,'{'+A+'}noFill')
 data[name]=E.tostring(root,encoding='utf-8',xml_declaration=True)
# EOT container embeds the original licensed Golos font without altering glyphs.
b=Path('app/static/golos.ttf').read_bytes();tabs={}
for i in range(struct.unpack('>H',b[4:6])[0]):
 tag,chk,off,l=struct.unpack('>4sIII',b[12+i*16:28+i*16]);tabs[tag]=b[off:off+l]
o=tabs[b'OS/2'];head=tabs[b'head'];names=['Golos Text','Regular','Version 1.000','Golos Text Regular']
h=bytearray(82);struct.pack_into('<IIII',h,0,0,len(b),0x00010000,0);h[16:26]=o[32:42];h[26]=1;h[27]=0;struct.pack_into('<IHH',h,28,400,struct.unpack('>H',o[8:10])[0],0x504c)
for j in range(4):struct.pack_into('<I',h,36+j*4,struct.unpack('>I',o[42+j*4:46+j*4])[0])
if len(o)>=86:
 for j in range(2):struct.pack_into('<I',h,52+j*4,struct.unpack('>I',o[78+j*4:82+j*4])[0])
struct.pack_into('<I',h,60,struct.unpack('>I',head[8:12])[0])
for i,v in enumerate(names):
 st=v.encode('utf-16le');h+=struct.pack('<H',len(st))+st
 if i<3:h+=b'\0\0'
eot=h+b;struct.pack_into('<I',eot,0,len(eot));data['ppt/fonts/GolosText.fntdata']=eot
pr=E.fromstring(data['ppt/presentation.xml']);pr.set('embedTrueTypeFonts','1');pr.set('saveSubsetFonts','0')
el=E.Element(q('embeddedFontLst')); anchor=pr.find(q('defaultTextStyle')); pr.insert(list(pr).index(anchor) if anchor is not None else len(pr),el);ef=E.SubElement(el,q('embeddedFont'));E.SubElement(ef,q('font'),{'typeface':'Golos Text'});E.SubElement(ef,q('regular'),{'{'+R+'}id':'rIdGolosFont'});data['ppt/presentation.xml']=E.tostring(pr,encoding='utf-8',xml_declaration=True)
relNS='http://schemas.openxmlformats.org/package/2006/relationships';rels=E.fromstring(data['ppt/_rels/presentation.xml.rels']);E.SubElement(rels,'{'+relNS+'}Relationship',{'Id':'rIdGolosFont','Type':R+'/font','Target':'fonts/GolosText.fntdata'});data['ppt/_rels/presentation.xml.rels']=E.tostring(rels,encoding='utf-8',xml_declaration=True)
E.register_namespace('', 'http://schemas.openxmlformats.org/package/2006/content-types');ct=E.fromstring(data['[Content_Types].xml']);E.SubElement(ct,'{http://schemas.openxmlformats.org/package/2006/content-types}Default',{'Extension':'fntdata','ContentType':'application/x-fontdata'});data['[Content_Types].xml']=E.tostring(ct,encoding='utf-8',xml_declaration=True)
with zipfile.ZipFile('.codex-presentation/build/animated.pptx','w',zipfile.ZIP_DEFLATED) as z:
 for n,v in data.items():z.writestr(n,v)
print('11 transitions;',count,'fade effects; Golos font embedded')
