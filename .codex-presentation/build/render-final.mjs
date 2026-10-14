import fs from 'node:fs/promises';
import {PresentationFile,FileBlob} from '@oai/artifact-tool';
const root='/Users/andreevich/Documents/Projects/codex/voice';
const p=await PresentationFile.importPptx(await FileBlob.load(root+'/output/presentation/Точка — презентация.pptx'));await fs.mkdir(root+'/.codex-presentation/build/final-render',{recursive:true});
for(let i=0;i<p.slides.items.length;i++){let b=await p.export({slide:p.slides.items[i],format:'png',scale:1});await fs.writeFile(root+`/.codex-presentation/build/final-render/${i+1}.png`,new Uint8Array(await b.arrayBuffer()));}console.log('Final rendered:',p.slides.items.length);
