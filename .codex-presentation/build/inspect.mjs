import fs from 'node:fs/promises';
import {PresentationFile,FileBlob} from '@oai/artifact-tool';
const p=await PresentationFile.importPptx(await FileBlob.load('/Users/andreevich/Downloads/Точка.pptx'));
console.log((await p.inspect({kind:'slide,layout',maxChars:10000})).ndjson);
await fs.mkdir('.codex-presentation/build/source',{recursive:true});
for(let i=0;i<p.slides.items.length;i++){
 try {const b=await p.export({slide:p.slides.items[i],format:'png',scale:0.8});await fs.writeFile(`.codex-presentation/build/source/${i+1}.png`,new Uint8Array(await b.arrayBuffer()));}catch(e){console.log('RENDER ERROR',i,e.message);}
}
